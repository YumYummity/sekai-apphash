import zipfile
import UnityPy
import logging
import json
import sys

from io import BytesIO

from tqdm import tqdm

REGION_MAP = {
    # NOTE: Order is used to determine the region
    "com.hermes.mk": "cn",
    "com.hermes.mk.asia": "tw",
    "com.sega.ColorfulStage.en": "en",
    "com.sega.pjsekai": "jp",
    "com.pjsekai.kr": "kr",
}
ROW_REGIONS = {"cn", "tw", "kr"}
logger = logging.getLogger("apphash")

PROD_BUNDLES = {
    "6350e2ec327334c8a9b7f494f344a761",  # PJSK Android
    "c726e51b6fe37463685916a1687158dd",  # PJSK iOS
    "data.unity3d",  # TW,KR,CN (ByteDance)
}


def enum_candidates(zip_file, filter):
    return (
        (f, zip_file.open(f), zip_file) for f in zip_file.filelist if filter(f.filename)
    )


def enum_package(zip_file):
    yield zip_file
    for f in zip_file.filelist:
        if f.filename.lower().endswith(".apk"):
            yield zipfile.ZipFile(zip_file.open(f))


def parse_axml_manifest(f: BytesIO):
    """Parse a binary AndroidManifest.xml (AXML) and return:
      - strings:      list of all string-pool entries
      - version_name: android:versionName from <manifest> (str or None)
      - version_code: android:versionCode from <manifest> (int or None)

    Binary AXML layout (all little-endian):
      File header  : type(u16) hdrSize(u16) fileSize(u32)
      String pool  : type(u16) hdrSize(u16) chunkSize(u32) nStrings(u32)
                     nStyles(u32) flags(u32) stringsStart(u32) stylesStart(u32)
                     offsets[nStrings](u32)  <string data>
      XML chunks   : type(u16) hdrSize(u16) chunkSize(u32) ...
        START_ELEMENT (0x0102):
          lineNumber(u32) comment(s32) ns(s32) name(s32)
          attrStart(u16) attrSize(u16) attrCount(u16) idIdx(u16) classIdx(u16) styleIdx(u16)
          attrs[attrCount]: ns(s32) name(s32) rawValue(s32) valueType(u32) valueData(s32)
            valueType high-byte: 0x03=TYPE_STRING  0x10=TYPE_INT_DEC  0x11=TYPE_INT_HEX
    """
    data = f.read()
    buf = BytesIO(data)
    read_int  = lambda n: int.from_bytes(buf.read(n), "little")
    read_sint = lambda n: int.from_bytes(buf.read(n), "little", signed=True)

    # File header
    buf.seek(0)
    read_int(2)  # file type  = 0x0003
    read_int(2)  # header size = 0x0008
    read_int(4)  # file size

    # String pool
    sp_start  = buf.tell()
    read_int(2)              # chunk type = 0x0001
    read_int(2)              # header size
    sp_size   = read_int(4)
    n_strings = read_int(4)
    read_int(4)              # n_styles
    flags     = read_int(4)
    is_utf8   = bool(flags & (1 << 8))
    str_data  = read_int(4) + sp_start   # absolute offset to string bytes
    read_int(4)              # styles start (unused)
    offsets   = [read_int(4) for _ in range(n_strings)]

    strings: list[str] = []
    for off in offsets:
        buf.seek(str_data + off)
        if is_utf8:
            # Two length prefixes: UTF-16 char count then UTF-8 byte count (each 1-or-2 bytes)
            b = read_int(1)
            if b & 0x80:
                b = ((b & 0x7F) << 8) | read_int(1)
            byte_len = read_int(1)
            if byte_len & 0x80:
                byte_len = ((byte_len & 0x7F) << 8) | read_int(1)
            strings.append(buf.read(byte_len).decode("utf-8", errors="replace"))
        else:
            char_len = read_int(2)
            if char_len & 0x8000:
                char_len = ((char_len & 0x7FFF) << 16) | read_int(2)
            strings.append(buf.read(char_len * 2).decode("utf-16-le", errors="replace"))

    # walk XML event chunks looking for the <manifest> START_ELEMENT
    ANDROID_NS = "http://schemas.android.com/apk/res/android"
    version_name: str | None = None
    version_code: int | None = None

    buf.seek(sp_start + sp_size)
    while True:
        chunk_start = buf.tell()
        header = buf.read(4)
        if len(header) < 4:
            break
        chunk_type = int.from_bytes(header[:2], "little")
        chunk_size = read_int(4)
        if chunk_size < 8:
            break

        if chunk_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
            read_int(4)           # lineNumber
            read_sint(4)          # comment
            read_sint(4)          # namespace index
            name_idx   = read_sint(4)
            read_int(2)           # attributeStart
            read_int(2)           # attributeSize
            attr_count = read_int(2)
            read_int(2)           # idIndex
            read_int(2)           # classIndex
            read_int(2)           # styleIndex

            elem_name = strings[name_idx] if 0 <= name_idx < len(strings) else ""

            for _ in range(attr_count):
                ns_idx    = read_sint(4)
                name_idx_ = read_sint(4)
                read_sint(4)          # rawValue
                val_type  = read_int(4)
                val_data  = read_sint(4)

                if elem_name != "manifest":
                    continue
                ns   = strings[ns_idx]    if 0 <= ns_idx    < len(strings) else ""
                name = strings[name_idx_] if 0 <= name_idx_ < len(strings) else ""
                if ns != ANDROID_NS:
                    continue
                type_byte = (val_type >> 24) & 0xFF
                if name == "versionName":
                    # TYPE_STRING (0x03): val_data is a string-pool index
                    if type_byte == 0x03 and 0 <= val_data < len(strings):
                        version_name = strings[val_data]
                    else:
                        version_name = str(val_data)
                elif name == "versionCode":
                    # TYPE_INT_DEC (0x10) or TYPE_INT_HEX (0x11)
                    version_code = val_data

            if elem_name == "manifest":
                break  # <manifest> is always the first element; stop here

        buf.seek(chunk_start + chunk_size)

    return strings, version_name, version_code


def main_apphash(args):
    UnityPy.config.SERIALIZED_FILE_PARSE_TYPETREE = False
    env = UnityPy.Environment()
    app_package = None

    apk_version = None  # {versionName}.{versionCode}, ROW only

    src = open(args.apk_src, "rb")
    with zipfile.ZipFile(src, "r") as zip_ref:
        manifests = [
            manifest
            for package in enum_package(zip_ref)
            for manifest in enum_candidates(
                package, lambda fn: fn == "AndroidManifest.xml"
            )
        ]
        manifest_bytes = BytesIO(manifests[0][1].read())
        manifest_strings, version_name, version_code = parse_axml_manifest(manifest_bytes)
        manifest_string_set = set(manifest_strings)
        # Heur: Reverse lookup the package name from the manifest strings
        for ky in REGION_MAP:
            if ky in manifest_string_set:
                app_package = ky
                break

        # ROW builds (CN/TW/KR) use APK versionName/versionCode
        # it's the app version required for requests (e.g. "6.0.0" / 20107 -> "6.0.0.20107")
        if REGION_MAP.get(app_package) in ROW_REGIONS and version_name and version_code is not None:
            apk_version = f"{version_name}.{version_code}"
        else:
            apk_version = None

        candidate_filter = lambda fn: fn.split("/")[-1] in PROD_BUNDLES
        candidate_filter_deep = lambda fn: ("_/" + fn).split("/")[-2] == "Data"

        candidates = [
            candidate
            for package in enum_package(zip_ref)
            for candidate in enum_candidates(
                package,
                candidate_filter_deep if args.deep else candidate_filter,
            )
        ]
        for candidate, stream, _ in tqdm(candidates, desc="Loading"):
            env.load_file(stream.read())

    from sssekai.generated import UTTCGen_AsInstance
    from sssekai.generated.Sekai import (
        AndroidPlayerSettingConfig,
        IOSPlayerSettingConfig,
    )

    res = dict()
    for reader in tqdm(env.objects, desc="Processing"):
        if reader.container:
            logger.info("Processing %s (%s)", reader.container, reader.type)
        if reader.type == UnityPy.enums.ClassIDType.MonoBehaviour:
            mono = reader.read(check_read=False)
            clazz = None
            platform = None
            if "_android" in mono.m_Name:
                clazz = AndroidPlayerSettingConfig
                platform = "android"
            elif "_ios" in mono.m_Name:
                clazz = IOSPlayerSettingConfig
                platform = "ios"
            if clazz:
                # Works with post 3.4 (JP 3rd Anniversary) builds and downstream regional builds
                try:
                    config = UTTCGen_AsInstance(clazz, reader)
                except Exception as e:
                    logger.error("Failed to parse config for %s: %s", mono.m_Name, e)
                    continue
                config: AndroidPlayerSettingConfig | IOSPlayerSettingConfig
                app_version = "%s.%s.%s" % (
                    config.clientMajorVersion,
                    config.clientMinorVersion,
                    config.clientBuildVersion,
                )
                data_version = "%s.%s.%s" % (
                    config.clientDataMajorVersion,
                    config.clientDataMinorVersion,
                    config.clientDataBuildVersion,
                )
                ab_version = "%s.%s.%s" % (
                    config.clientMajorVersion,
                    config.clientMinorVersion,
                    config.clientDataRevision,
                )
                app_hash = config.clientAppHash
                package_heur = app_package or config.bundleIdentifier
                region = REGION_MAP.get(package_heur, "unknown")
                print(
                    f"Found {config.productName} at {config.m_Name}",
                    f"  Memo: {config.memo}",
                    f"  Package: {config.bundleIdentifier} (actually assumed as {package_heur})",
                    f"  Platform: {platform}",
                    f"  AppHash (app_hash):     {config.clientAppHash}",
                    f"  Region  (app_region):   {region} (determined by {package_heur})",
                    f"  Version (app_version):  {app_version}",
                    f"  Version (ab_version):   {ab_version}",
                    f"  Bundle Version: {config.bundleVersion}",
                    f"  Data Version:   {data_version}",
                    f"  Version Suffix: {config.clientVersionSuffix}",
                    "",
                    "",
                    sep="\n",
                    file=sys.stderr,
                )
                app_package = app_package or "Unknown Package (Failed APK Heuristic)"
                # fmt: off
                match args.format:
                    case 'json':
                        res[mono.m_Name] = {
                            "package": app_package,
                            "reported_package": config.bundleIdentifier,
                            "app_hash": app_hash,
                            "app_region": region,
                            "app_version": app_version,
                            "app_platform": platform,
                            "ab_version": ab_version,
                        }
                    case "markdown":
                        res[mono.m_Name] = f"""{app_package} ({app_version}, {region})
---
Reported Package: {config.bundleIdentifier}

|{'app_hash'.rjust(48)}|   app_region|  app_version|   ab_version|
|{'-'.rjust(48, '-')}|-------------|-------------|-------------|
|{app_hash.rjust(48)}|{region.rjust(13)}|{app_version.rjust(13)}|{ab_version.rjust(13)}|

- CLI Usage:

        sssekai abcache --app-platform {platform} --app-region {region} --app-version {app_version} --app-appHash {app_hash} --app-abVersion {ab_version}

- Python Usage:

        from sssekai.abcache import AbCacheConfig

        AbCacheConfig(
            app_region="{region}",
            app_version="{app_version}",
            ab_version="{ab_version}",
            app_hash="{app_hash}",
            app_platform="{platform}"
        )
"""
    print("###### RESULTS ######", file=sys.stderr)
    res = dict(sorted(res.items(), key=lambda x: x[0]))
    match args.format:
        case "json":
            output = {}
            if apk_version is not None:
                output["apk_version"] = apk_version
            output.update(res)
            print(json.dumps(output, indent=4, ensure_ascii=False))
        case "markdown":
            if apk_version is not None:
                print(f"APK Reported Version: {apk_version}\n")
            for name, content in res.items():
                print(f"## {name}\n{content}\n")
