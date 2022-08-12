import argparse
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List
from urllib.parse import ParseResult, urlparse

import requests
import sentry_sdk


IOS_UTILS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ios-utils")


@dataclass
class Device:
    identifier: str
    name: str
    architecture: str


DEVICES_TO_CHECK: Dict[str, List[Device]] = {
    "ios": [
        Device(identifier="iPhone14,2", name="iPhone 13 Pro", architecture="arm64e"),
        Device(identifier="iPhone8,1", name="iPhone 6S", architecture="arm64"),
        Device(identifier="iPad12,1", name="iPad 9", architecture="arm64e"),
        Device(identifier="iPad5,4", name="iPad Air 2", architecture="arm64"),
    ],
    "tvos": [
        Device(identifier="AppleTV5,3", name="AppleTV 4 (2015)", architecture="arm64"),
    ],
    "macos": [
        Device(
            identifier="MacBookPro18,3",
            name="MacBook Pro (M1 Pro, 14-inch, 2021)",
            architecture="arm64e",
        ),
    ],
    "watchos": [],
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


def main():
    logging.basicConfig(level=logging.INFO, format="[sentry] %(message)s")
    parser = argparse.ArgumentParser(
        description="Downloads new iOS firmware, extracts symbols, and uploads them to Cloud Storage"
    )
    parser.add_argument("--os_name", help="The OS name to check for")
    parser.add_argument(
        "--os_version",
        default="latest",
        help="The version of iOS to request IPSWs for (defaults to latest)",
    )
    args = parser.parse_args()

    if args.os_name is None:
        sys.exit("You need to specify an OS name to check for.")

    main_download_otas(args.os_name, args.os_version)
    main_download_ipsws(args.os_name, args.os_version)


def main_download_otas(os_name: str, os_version: str):
    with sentry_sdk.start_transaction(
        op="task", name="import symbols from OTA archive"
    ) as transaction:
        with sentry_sdk.start_transaction(op="task", name="checking OTAs") as transaction:
            with transaction.start_child(op="task", description="Check for new versions") as span:
                otas = get_missing_ota_only_releases(os_name, os_version)
                if len(otas) == 0:
                    return
                span.set_data("new_archives", otas)

        with tempfile.TemporaryDirectory(prefix="_sentry_symcache_output_") as symcache_output:
            with tempfile.TemporaryDirectory(prefix="_sentry_ota_archives_") as ota_dir:
                for ota in otas:
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

            with transaction.start_child(op="task", description="Upload symbols to GCS bucket"):
                upload_to_gcs(symcache_output)


def main_download_ipsws(os_name: str, os_version: str):
    with sentry_sdk.start_transaction(
        op="task", name="import symbols from IPSW archive"
    ) as transaction:
        with transaction.start_child(op="task", description="Check for new versions") as span:
            ipsws = get_missing_ipsws(os_name, os_version)
            if len(ipsws) == 0:
                return
            span.set_data("new_archives", ipsws)

        with tempfile.TemporaryDirectory(prefix="_sentry_symcache_output_") as symcache_output:
            with tempfile.TemporaryDirectory(prefix="_sentry_ipsw_archives_") as ipsw_dir:
                for ipsw in ipsws:
                    with transaction.start_child(
                        op="task", description="Process IPSW archive"
                    ) as ipsw_span:
                        for k, v in asdict(ipsw).items():
                            ipsw_span.set_data(k, v)

                        with ipsw_span.start_child(
                            op="task", description="Download new version"
                        ) as span:
                            local_path = os.path.join(ipsw_dir, os.path.basename(ipsw.url.path))
                            url = ipsw.url.geturl()
                            span.set_data("url", url)
                            download_archive(url, local_path)
                        with ipsw_span.start_child(
                            op="task", description="Extract symbols from archive"
                        ) as span:
                            with tempfile.TemporaryDirectory(
                                prefix="_sentry_ipsw_extract_dir_"
                            ) as extract_dir:
                                extract_symbols_from_one_ipsw_archive(
                                    local_path,
                                    extract_dir,
                                    symcache_output,
                                    ipsw.os_name,
                                    ipsw.architecture,
                                )
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
    plist_path = os.path.join(extract_dir, "Restore.plist")
    (restore_images, os_version, build_number) = read_restore_plist(plist_path)

    # Use the first one only since the rest is the otaecovery OS
    system_restore_image_filename = list(restore_images.keys())[0]
    restore_image_path = os.path.join(extract_dir, system_restore_image_filename)

    logging.info(f"Mounting {restore_image_path}")
    with span.start_child(op="task", description="Mount archive"):
        volume_path = (
            subprocess.check_output(
                [f"hdiutil attach {restore_image_path} | grep /Volumes/ | cut -f 3"], shell=True
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
        uncompressed_payload = os.path.join(level1_extract_dir, "uncompressed_payload")

        with span.start_child(op="task", description="Extract OTA archive"):
            extract_zip_archive(ota_archive_path, level1_extract_dir)
        with span.start_child(op="task", description="Decompress payloadv2"):
            decompress_payloadv2(
                os.path.join(level1_extract_dir, "AssetData", "payloadv2", "payload"),
                uncompressed_payload,
            )

        with tempfile.TemporaryDirectory(
            prefix="_sentry_final_ota_extract_dir_"
        ) as level2_extract_dir:
            with span.start_child(op="task", description="Unpack OTA from payload"):
                unpack_ota(uncompressed_payload, level2_extract_dir)

            shared_cache_dir = os.path.join(
                level2_extract_dir,
                "System",
                "Library",
                "Caches",
                "com.apple.dyld",
            )

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


def decompress_payloadv2(payload_path: str, output_path: str) -> None:
    logging.info(f"Uncompressing {payload_path}")
    with open(payload_path, "rb") as payload:
        with open(output_path, "wb") as output:
            subprocess.check_call(
                [os.path.join(IOS_UTILS_DIR, "pbzx"), payload_path],
                stdin=payload,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )


def unpack_ota(payload_path: str, output_path: str) -> None:
    logging.info(f"Unpacking OTA from {payload_path}")
    subprocess.check_call(
        [os.path.join(IOS_UTILS_DIR, "ota"), "-e", payload_path],
        cwd=output_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


def read_restore_plist(plist_path: str):
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
        restore_images = plist["SystemRestoreImageFileSystems"]
        build_number = plist["ProductBuildVersion"]
        os_version = plist["ProductVersion"]
        logging.info(
            f"Found image for {os_version} ({build_number}) in {os.path.dirname(plist_path)}"
        )
    return restore_images, os_version, build_number


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
            res = requests.get(f"https://api.ipsw.me/v4/device/{device.identifier}?type=ota")
            res.raise_for_status()

            qualifying_firmwares = sorted(
                [x for x in res.json()["firmwares"] if x["releasetype"] != "Beta"],
                key=lambda x: parse_date(x["releasedate"]),
            )

            if not qualifying_firmwares:
                continue

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

            with span.start_child(
                op="task", description="Check if OTA version has symbols already"
            ) as symbols_span:
                if has_symbols_in_cloud_storage(ota.os_name, ota.bundle_id):
                    symbols_span.set_data("has_symbols_in_cloud_storage", True)
                    logging.info(f"We already have symbols for {ota.bundle_id}")
                    continue

            rv.append(ota)

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

            with device_span.start_child(
                op="task", description="Check if version has symbols already"
            ) as symbols_span:
                if has_symbols_in_cloud_storage(ipsw.os_name, ipsw.bundle_id):
                    symbols_span.set_data("has_symbols_in_cloud_storage", True)
                    logging.info(f"We already have symbols for {ipsw.bundle_id}")
                    continue

            build_key = f"{os_name}-{latest_os_version}-{latest_build_number}-{device.architecture}"
            if build_to_ipsw.get(build_key):
                continue

            build_to_ipsw[build_key] = ipsw
    return list(build_to_ipsw.values())


def has_symbols_in_cloud_storage(prefix: str, bundle_id: str) -> bool:
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
    print(result.stdout)
    result.check_returncode()
    return False


if __name__ == "__main__":
    sentry_sdk.init(
        dsn="https://f86a0e29c86e49688d691e194c5bf9eb@o1.ingest.sentry.io/6418660",
        traces_sample_rate=1.0,
    )
    main()
