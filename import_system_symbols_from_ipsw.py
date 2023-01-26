import logging
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import ParseResult, urlparse

import click
import requests
import sentry_sdk
from packaging import version

_ota_payload_pattern = re.compile(r"^payload\.[0-9]{3}$")

IOS_UTILS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ios-utils")


@dataclass
class Device:
    identifier: str
    name: str
    architecture: str


DEVICES_TO_CHECK: Dict[str, List[Device]] = {
    "ios": [
        Device(identifier="iPhone14,2", name="iPhone 13 Pro", architecture="arm64e"),
        Device(identifier="iPhone10,6", name="iPhone X (GSM)", architecture="arm64"),
        Device(identifier="iPhone8,1", name="iPhone 6S", architecture="arm64"),
        Device(identifier="iPad12,1", name="iPad 9", architecture="arm64e"),
        Device(identifier="iPad5,4", name="iPad Air 2", architecture="arm64"),
    ],
    "tvos": [
        Device(identifier="AppleTV5,3", name="AppleTV 4 (2015)", architecture="arm64"),
        Device(identifier="AppleTV6,2", name="AppleTV 4 (2015)", architecture="arm64"),
    ],
    "macos": [
        Device(
            identifier="MacBookPro18,3",
            name="MacBook Pro (M1 Pro, 14-inch, 2021)",
            architecture="arm64e",
        ),
        Device(
            identifier="MacBookAir10,1",
            name="MacBook Air (M1, Late 2020)",
            architecture="arm64e",
        ),
    ],
    "watchos": [
        Device(
            identifier="Watch5,4", name="Apple Watch Series 5 (44mm, LTE)", architecture="arm64e"
        ),
        Device(
            identifier="Watch4,3", name="Apple Watch Series 4 (40mm, LTE)", architecture="arm64e"
        ),
        Device(identifier="Watch3,4", name="Apple Watch Series 3 (42mm)", architecture="arm64e"),
    ],
}


@dataclass
class IPSW:
    architecture: str
    build_number: str
    os_name: str
    os_version: str
    url: ParseResult

    @property
    def bundle_id(self) -> str:
        return f"{self.os_version}_{self.build_number}_{self.architecture}"


@dataclass
class OTA:
    build_number: str
    device_identifier: str
    os_name: str
    os_version: str
    url: ParseResult

    @property
    def bundle_id(self) -> str:
        return f"{self.device_identifier}_{self.os_version}_{self.build_number}_ota"


@click.command()
@click.option("--os-name", help="The os name to check for", required=True)
@click.option(
    "--os-version",
    default="latest",
    help=(
        "The version of iOS to request IPSWs/OTAs for (defaults to latest). "
        "For OTAs this can be set to 'all' to force all to download."
    ),
)
@click.option(
    "--type",
    type=click.Choice(["ota", "ipsw"]),
    default=("ipsw",),
    multiple=True,
    help="The type of firmware to download. Defaults to ipsw.",
)
@click.option("--no-upload", is_flag=True, help="Don't upload the symbols to GCS")
def main(os_name, os_version, type, no_upload):
    logging.basicConfig(level=logging.INFO, format="[sentry] %(message)s")

    if "ota" in type:
        main_download_otas(os_name, os_version, not no_upload)
    if "ipsw" in type:
        main_download_ipsws(os_name, os_version, not no_upload)


def main_download_otas(os_name: str, os_version: str, upload: bool = True):
    with sentry_sdk.start_transaction(
        op="task", name="import symbols from OTA archive"
    ) as transaction:
        with sentry_sdk.start_transaction(op="task", name="Checking OTAs") as transaction:
            with transaction.start_child(op="task", description="Check for new versions") as span:
                otas = get_missing_ota_only_releases(os_name, os_version)
                if len(otas) == 0:
                    return
                span.set_data("new_archives", otas)

        with tempfile.TemporaryDirectory(prefix="_sentry_symcache_output_") as symcache_output:
            with tempfile.TemporaryDirectory(prefix="_sentry_ota_archives_") as ota_dir:
                for ota in otas:
                    try:
                        with transaction.start_child(
                            op="task", description="Process OTA archive"
                        ) as ota_span:
                            for k, v in asdict(ota).items():
                                ota_span.set_data(k, v)

                            with ota_span.start_child(
                                op="task", description="Download new version"
                            ) as span:
                                local_path = os.path.join(ota_dir, os.path.basename(ota.url.path))
                                url = ota.url.geturl()
                                span.set_data("url", url)
                                download_archive(url, local_path)

                            with ota_span.start_child(
                                op="task", description="Extract symbols from archive"
                            ) as span:
                                extract_symbols_from_one_ota_archive(
                                    local_path,
                                    symcache_output,
                                    os_name,
                                    ota.bundle_id,
                                )
                    except Exception as e:
                        if os_version == "all":
                            logging.error(
                                "Failed to process OTA archive: %s", e, exc_info=sys.exc_info()
                            )

            if upload:
                with transaction.start_child(op="task", description="Upload symbols to GCS bucket"):
                    upload_to_gcs(symcache_output)


def main_download_ipsws(os_name: str, os_version: str, upload: bool = True):
    with sentry_sdk.start_transaction(
        op="task", name="import symbols from IPSW archive"
    ) as transaction:
        with transaction.start_child(op="task", description="Check for new versions") as span:
            downloads, ipsws = get_missing_ipsws(os_name, os_version)
            if len(downloads) == 0 or len(ipsws) == 0:
                return
            span.set_data("new_archives", ipsws)

        with tempfile.TemporaryDirectory(prefix="_sentry_symcache_output_") as symcache_output:
            with tempfile.TemporaryDirectory(prefix="_sentry_ipsw_archives_") as ipsw_dir:
                for raw_url in downloads.values():
                    with transaction.start_child(
                        op="task", description="Download new IPSW archive"
                    ) as span:
                        local_path = os.path.join(ipsw_dir, os.path.basename(raw_url.path))
                        url = raw_url.geturl()
                        span.set_data("url", url)
                        download_archive(url, local_path)

                for ipsw in ipsws:
                    with transaction.start_child(
                        op="task", description="Process IPSW archive"
                    ) as ipsw_span:
                        for k, v in asdict(ipsw).items():
                            ipsw_span.set_data(k, v)
                        with ipsw_span.start_child(
                            op="task", description="Extract symbols from archive"
                        ) as span:
                            with tempfile.TemporaryDirectory(
                                prefix="_sentry_ipsw_extract_dir_"
                            ) as extract_dir:
                                extract_symbols_from_one_ipsw_archive(
                                    os.path.join(ipsw_dir, os.path.basename(ipsw.url.path)),
                                    extract_dir,
                                    symcache_output,
                                    ipsw.os_name,
                                    ipsw.architecture,
                                )

            if upload:
                with transaction.start_child(op="task", description="Upload symbols to GCS bucket"):
                    upload_to_gcs(symcache_output)


def download_archive(url: str, filepath: str) -> None:
    logging.info(f"Downloading {url}")
    r = requests.get(url, stream=True)
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def extract_symbols_from_one_ipsw_archive(
    ipsw_archive_path: str,
    extract_dir: str,
    symcache_output_path: str,
    prefix: str,
    architecture: str,
) -> None:
    span = sentry_sdk.Hub.current.scope.span
    with span.start_child(op="task", description="Extract IPSW archive"):
        extract_zip_archive(ipsw_archive_path, extract_dir)

    if prefix == "macos":
        os_version, build_number = read_system_version_plist(extract_dir)
    else:
        os_version, build_number = read_version_from_restore_plist(extract_dir)

    parsed_version = version.parse(os_version)
    logging.info(f"Found image for {os_version} ({build_number}) in {extract_dir}")

    # Starting iOS 16.0 and macOS 13.0, dyld caches are in a different image
    if (
        prefix == "macos"
        and parsed_version >= version.parse("13.0")
        or prefix == "ios"
        and parsed_version >= version.parse("16.0")
    ):
        system_restore_image_filename = read_build_manifest_plist(extract_dir)
        with span.start_child(op="task", description="Process one dmg"):
            process_one_dmg(
                extract_dir,
                symcache_output_path,
                prefix,
                architecture,
                system_restore_image_filename,
                os_version,
                build_number,
            )
    else:
        for system_restore_image_filename in read_restore_plist(extract_dir):
            with span.start_child(op="task", description="Process one dmg"):
                process_one_dmg(
                    extract_dir,
                    symcache_output_path,
                    prefix,
                    architecture,
                    system_restore_image_filename,
                    os_version,
                    build_number,
                )


def process_one_dmg(
    extract_dir,
    symcache_output_path,
    prefix,
    architecture,
    system_restore_image_filename,
    os_version,
    build_number,
):
    restore_image_path = os.path.join(extract_dir, system_restore_image_filename)
    span = sentry_sdk.Hub.current.scope.span

    logging.info(f"Mounting {restore_image_path}")
    with span.start_child(op="task", description="Mount archive"):
        volume_path = (
            subprocess.check_output(
                [f"hdiutil attach {restore_image_path} | grep /Volumes/ | cut -f 3"],
                shell=True,
            )
            .decode("utf-8")
            .strip()
        )

    try:
        bundle_id = f"{os_version}_{build_number}_{architecture}"
        span.set_data("bundle_id", bundle_id)
        if prefix == "macos":
            shared_cache_dir = os.path.join(
                volume_path,
                "System",
                "Library",
                "dyld",
            )
        else:
            shared_cache_dir = os.path.join(
                volume_path,
                "System",
                "Library",
                "Caches",
                "com.apple.dyld",
            )
        for filename in os.listdir(shared_cache_dir):
            # iOS 15.0+ firmwares have multiple dyld_shared_cache files for the same architecture,
            # e.g. dyld_shared_cache_arm64e.1, dyld_shared_cache_arm64e.2, etc.
            # We can ignore these: https://github.com/keith/dyld-shared-cache-extractor/issues/1#issuecomment-924265280
            #
            # To extract these, Xcode 13.0+ needs to be the selected Xcode version.
            if not filename.startswith("dyld_shared_cache") or os.path.splitext(filename)[1] != "":
                continue
            process_shared_cache_file(
                filename, shared_cache_dir, prefix, bundle_id, symcache_output_path
            )

        symsort_utilities(volume_path, prefix, bundle_id, symcache_output_path)
    finally:
        logging.info(f"Unmounting {restore_image_path}")
        with span.start_child(op="task", description="Unmount archive"):
            subprocess.check_call(
                ["hdiutil", "detach", volume_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def extract_symbols_from_one_ota_archive(
    ota_archive_path: str,
    symcache_output_path: str,
    prefix: str,
    bundle_id: str,
) -> None:
    span = sentry_sdk.Hub.current.scope.span
    with tempfile.TemporaryDirectory(prefix="_sentry_ota_extract_dir_") as level1_extract_dir:
        with span.start_child(op="task", description="Extract OTA archive"):
            extract_zip_archive(ota_archive_path, level1_extract_dir)

        payloadv2 = os.path.join(level1_extract_dir, "AssetData", "payloadv2")

        with tempfile.TemporaryDirectory(
            prefix="_sentry_final_ota_extract_dir_"
        ) as level2_extract_dir:
            with span.start_child(op="task", description="Unpack OTA from payload"):
                unpack_ota(payloadv2, level2_extract_dir)

            shared_cache_dir = os.path.join(
                level2_extract_dir,
                "System",
                "Library",
                "Caches",
                "com.apple.dyld",
            )

            if not os.path.isdir(shared_cache_dir):
                logging.warning(f"No dyld shared cache found in {shared_cache_dir}")
                return

            for filename in os.listdir(shared_cache_dir):
                if (
                    not filename.startswith("dyld_shared_cache")
                    or os.path.splitext(filename)[1] != ""
                ):
                    continue
                process_shared_cache_file(
                    filename, shared_cache_dir, prefix, bundle_id, symcache_output_path
                )

            symsort_utilities(level2_extract_dir, prefix, bundle_id, symcache_output_path)


def unpack_ota(payload_path: str, output_path: str) -> None:
    logging.info(f"Unpacking OTA from {payload_path}")

    def run_ota(path):
        try:
            subprocess.check_call(
                [os.path.join(IOS_UTILS_DIR, "ota"), "-e", "*", path],
                cwd=output_path,
            )
        except subprocess.CalledProcessError as err:
            logging.error("Failed to unpack OTA payload: %s", err, exc_info=sys.exc_info())

    single_payload = os.path.join(payload_path, "payload")
    if os.path.isfile(single_payload):
        run_ota(single_payload)

    files = [x for x in os.listdir(payload_path) if _ota_payload_pattern.match(x)]
    files.sort()

    for filename in files:
        run_ota(os.path.join(payload_path, filename))


def process_shared_cache_file(
    filename: str, shared_cache_dir: str, prefix: str, bundle_id: str, symcache_output_path: str
) -> None:
    span = sentry_sdk.Hub.current.scope.span
    with tempfile.TemporaryDirectory(prefix="_sentry_dylib_cache_output") as output_path:
        with span.start_child(
            op="task", description="Process shared cache file"
        ) as shared_cache_span:
            shared_cache_span.set_data("shared_cache_file", filename)
            cache_path = os.path.join(shared_cache_dir, filename)
            logging.info(f"Extracting {cache_path} to {output_path}")
            with shared_cache_span.start_child(
                op="task", description="Run dyld-shared-cache-extractor"
            ):
                subprocess.check_call(["dyld-shared-cache-extractor", cache_path, output_path])
            with shared_cache_span.start_child(
                op="task", description="Run symsorter for shared cache directory"
            ):
                symsorter(symcache_output_path, prefix, bundle_id, output_path)


def symsort_utilities(
    volume_path: str, prefix: str, bundle_id: str, symcache_output_path: str
) -> None:
    span = sentry_sdk.Hub.current.scope.span
    other_dylib_paths = [
        os.path.join(volume_path, "usr", "lib"),
        os.path.join(volume_path, "System", "Library", "AccessibilityBundles"),
    ]
    for dylib_path in other_dylib_paths:
        with span.start_child(
            op="task", description="Run symsorter for other dylib paths"
        ) as other_path_span:
            other_path_span.set_data("dylib_path", dylib_path)
            symsorter(symcache_output_path, prefix, bundle_id, dylib_path)


def symsorter(output_path: str, prefix: str, bundle_id: str, input_path: str) -> None:
    subprocess.check_call(
        [
            "./symsorter",
            "-zz",
            "--ignore-errors",
            "-o",
            output_path,
            "--prefix",
            prefix,
            "--bundle-id",
            bundle_id,
            input_path,
        ]
    )


def extract_zip_archive(archive_path: str, extract_dir: str) -> None:
    logging.info(f"Extracting {archive_path} to {extract_dir}")
    subprocess.check_call(
        ["unzip", archive_path, "-d", extract_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def read_system_version_plist(extract_dir: str) -> Tuple[str, str]:
    with open(os.path.join(extract_dir, "SystemVersion.plist"), "rb") as f:
        plist = plistlib.load(f)
        return plist["ProductVersion"], plist["ProductBuildVersion"]


def read_build_manifest_plist(extract_dir: str) -> str:
    with open(os.path.join(extract_dir, "BuildManifest.plist"), "rb") as f:
        plist = plistlib.load(f)
        return plist["BuildIdentities"][0]["Manifest"]["Cryptex1,SystemOS"]["Info"]["Path"]


def read_restore_plist(extract_dir: str) -> List[str]:
    with open(os.path.join(extract_dir, "Restore.plist"), "rb") as f:
        plist = plistlib.load(f)
        return list(plist["SystemRestoreImageFileSystems"].keys())


def read_version_from_restore_plist(extract_dir: str) -> Tuple[str, str]:
    with open(os.path.join(extract_dir, "Restore.plist"), "rb") as f:
        plist = plistlib.load(f)
        return plist["ProductVersion"], plist["ProductBuildVersion"]


def upload_to_gcs(symcache_dir: str):
    if not any(Path(symcache_dir).iterdir()):
        logging.info(f"Directory {symcache_dir} is empty, nothing to do.")
        return
    logging.info("Uploading symcache artifacts to production symbols bucket")
    subprocess.check_call(
        ["gsutil", "-m", "cp", "-rn", ".", "gs://sentryio-system-symbols-0"], cwd=symcache_dir
    )


def parse_date(date: str) -> datetime:
    return datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ")


def get_missing_ota_only_releases(os_name: str, version: str) -> List[OTA]:
    versions = {}

    span = sentry_sdk.Hub.current.scope.span
    for device in DEVICES_TO_CHECK.get(os_name, []):
        with span.start_child(op="http.client", description="Fetch all versions"):
            logging.info(f"Finding OTA releases for {device.identifier}")
            res = requests.get(f"https://api.ipsw.me/v4/device/{device.identifier}?type=ota")
            res.raise_for_status()

            qualifying_firmwares = sorted(
                (
                    x
                    for x in res.json()["firmwares"]
                    if x["releasetype"] != "Beta"
                    and not x["prerequisitebuildid"]
                    and not x["prerequisiteversion"]
                ),
                key=lambda x: parse_date(x["releasedate"]),
            )

            if not qualifying_firmwares:
                continue

            if version == "all":
                firmwares = qualifying_firmwares
            else:
                if version == "latest":
                    actual_version = qualifying_firmwares[-1]["version"]
                else:
                    actual_version = version
                firmwares = [x for x in qualifying_firmwares if x["version"] == actual_version]

            for firmware in firmwares:
                normal_version = regular_version_from_ota_version(firmware["version"])
                key = normal_version, firmware["buildid"]
                versions.setdefault(key, {})[firmware["url"]] = firmware

        with span.start_child(op="http.client", description="Fetch all IPSWs for diffing"):
            logging.info(f"Finding IPSW releases for {device.identifier} for diffing")
            res = requests.get(f"https://api.ipsw.me/v4/device/{device.identifier}?type=ipsw")
            res.raise_for_status()

            for firmware in res.json()["firmwares"]:
                normal_version = regular_version_from_ota_version(firmware["version"])
                versions.pop((firmware["version"], firmware["buildid"]), None)

    rv = []

    for version in versions.values():
        for info in version.values():
            ota = OTA(
                os_name=os_name,
                device_identifier=info["identifier"],
                build_number=info["buildid"],
                os_version=info["version"],
                url=urlparse(info["url"]),
            )

            logging.info(f"Check if we have {ota.bundle_id} on GCS")
            with span.start_child(
                op="task", description="Check if OTA version has symbols already"
            ) as symbols_span:
                if has_symbols_in_cloud_storage(ota.os_name, ota.bundle_id):
                    symbols_span.set_data("has_symbols_in_cloud_storage", True)
                    logging.info(f"We already have symbols for {ota.bundle_id}")
                    continue
                else:
                    logging.info(f"Need to download and process {ota.bundle_id}")

            rv.append(ota)

    logging.info("Missing versions %s", sorted({x.os_version for x in rv}))

    return rv


def regular_version_from_ota_version(ota_version: str) -> str:
    if ota_version.startswith("9.9."):
        return ota_version[4:]
    return ota_version


def get_missing_ipsws(os_name: str, os_version: str) -> List[IPSW]:
    if os_name not in DEVICES_TO_CHECK:
        return []

    span = sentry_sdk.Hub.current.scope.span
    build_to_ipsw: Dict[str, IPSW] = {}
    downloads: Dict[str, str] = {}
    for device in DEVICES_TO_CHECK.get(os_name, []):
        with span.start_child(op="http.client", description="Fetch latest versions") as device_span:
            res = requests.get(
                f"https://api.ipsw.me/v2.1/{device.identifier}/{os_version}/info.json"
            )
            res.raise_for_status()
            if len(res.json()) == 0:
                continue

            ipsw_info = res.json()[0]
            latest_os_version = ipsw_info["version"]
            latest_build_number = ipsw_info["buildid"]
            ipsw = IPSW(
                os_version=latest_os_version,
                build_number=latest_build_number,
                url=urlparse(ipsw_info["url"]),
                os_name=os_name,
                architecture=device.architecture,
            )
            device_span.set_data("latest_os_version", latest_os_version)
            device_span.set_data("latest_build_nunber", latest_build_number)

            logging.info(f"Check if we have {ipsw.bundle_id} on GCS")
            with device_span.start_child(
                op="task", description="Check if version has symbols already"
            ) as symbols_span:
                if has_symbols_in_cloud_storage(ipsw.os_name, ipsw.bundle_id):
                    symbols_span.set_data("has_symbols_in_cloud_storage", True)
                    logging.info(f"We already have symbols for {ipsw.bundle_id}")
                    continue
                else:
                    logging.info(f"Need to download and process {ipsw.bundle_id}")

            download_key = f"{os_name}-{latest_os_version}-{latest_build_number}"
            if download_key not in downloads:
                downloads[download_key] = ipsw.url

            build_key = f"{download_key}-{device.architecture}"
            if build_key in build_to_ipsw:
                continue

            build_to_ipsw[build_key] = ipsw
    return downloads, list(build_to_ipsw.values())


def has_symbols_in_cloud_storage(prefix: str, bundle_id: str) -> bool:
    return False
    storage_path = f"gs://sentryio-system-symbols-0/{prefix}/bundles/{bundle_id}"
    result = subprocess.run(
        ["gsutil", "stat", storage_path],
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode == 0:
        return True
    elif "No URLs matched" in result.stdout:
        return False
    # Fallback to raising an exception for other errors.
    result.check_returncode()
    return False


if __name__ == "__main__":
    sentry_sdk.init(
        dsn="https://f86a0e29c86e49688d691e194c5bf9eb@o1.ingest.sentry.io/6418660",
        traces_sample_rate=1.0,
    )
    main()
