#!/usr/bin/env python3

import argparse
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import ParseResult, urlparse

import requests


@dataclass
class Device:
    identifier: str
    name: str
    architecture: str


DEVICES_TO_CHECK: Dict[str, List[Device]] = {
    "ios": [
        # Device(identifier='iPhone14,2', name='iPhone 13 Pro', architecture='arm64e'),
        # Device(identifier='iPhone8,1', name='iPhone 6S', architecture='arm64'),
        # Device(identifier='iPad12,1', name='iPad 9', architecture='arm64e'),
        # Device(identifier='iPad5,4', name='iPad Air 2', architecture='arm64'),
    ],
    "tvos": [
        Device(identifier="AppleTV5,3", name="AppleTV 4 (2015)", architecture="arm64"),
    ],
    "macos": [],
    "watchos": [],
}


@dataclass
class IPSW:
    os_version: str
    build_number: str
    url: ParseResult
    architecture: str
    prefix: str

    @property
    def bundle_id(self) -> str:
        return f"{self.os_version}_{self.build_number}_{self.architecture}"


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

    ipsws = get_missing_ipsws(args.os_name, args.os_version)
    if len(ipsws) == 0:
        return

    with tempfile.TemporaryDirectory(prefix="_sentry_symcache_output_") as symcache_output:
        with tempfile.TemporaryDirectory(prefix="_sentry_ipsw_archives_") as ipsw_dir:
            for ipsw in ipsws:
                local_path = os.path.join(ipsw_dir, os.path.basename(ipsw.url.path))
                download_ipsw_archive(ipsw.url.geturl(), local_path)
                with tempfile.TemporaryDirectory(prefix="_sentry_ipsw_extract_dir_") as extract_dir:
                    extract_symbols_from_one_archive(
                        local_path, extract_dir, symcache_output, ipsw.prefix, ipsw.architecture
                    )
        upload_to_gcs(symcache_output)


def download_ipsw_archive(url: str, filepath: str) -> None:
    logging.info(f"Downloading {url}")
    r = requests.get(url, stream=True)
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def extract_symbols_from_one_archive(
    ipsw_archive_path: str,
    extract_dir: str,
    symcache_output_path: str,
    prefix: str,
    architecture: str,
) -> None:
    extract_ipsw_archive(ipsw_archive_path, extract_dir)
    plist_path = os.path.join(extract_dir, "Restore.plist")
    (restore_images, os_version, build_number) = read_restore_plist(plist_path)

    # Use the first one only since the rest is the recovery OS
    system_restore_image_filename = list(restore_images.keys())[0]
    restore_image_path = os.path.join(extract_dir, system_restore_image_filename)

    logging.info(f"Mounting {restore_image_path}")
    volume_path = (
        subprocess.check_output(
            [f"hdiutil attach {restore_image_path} | grep /Volumes/ | cut -f 3"], shell=True
        )
        .decode("utf-8")
        .strip()
    )

    try:
        bundle_id = f"{os_version}_{build_number}_{architecture}"
        shared_cache_dir = os.path.join(
            volume_path, "System", "Library", "Caches", "com.apple.dyld"
        )
        for filename in os.listdir(shared_cache_dir):
            # iOS 15.0+ firmwares have multiple dyld_shared_cache files for the same architecture,
            # e.g. dyld_shared_cache_arm64e.1, dyld_shared_cache_arm64e.2, etc.
            # We can ignore these: https://github.com/keith/dyld-shared-cache-extractor/issues/1#issuecomment-924265280
            #
            # To extract these, Xcode 13.0+ needs to be the selected Xcode version.
            if not filename.startswith("dyld_shared_cache") or os.path.splitext(filename)[1] != "":
                continue
            with tempfile.TemporaryDirectory(prefix="_sentry_dylib_cache_output") as output_path:
                cache_path = os.path.join(shared_cache_dir, filename)
                logging.info(f"Extracting {cache_path} to {output_path}")
                subprocess.check_call(["dyld-shared-cache-extractor", cache_path, output_path])
                subprocess.check_call(
                    [
                        "./symsorter",
                        "-zz",
                        "--ignore-errors",
                        "-o",
                        symcache_output_path,
                        "--prefix",
                        prefix,
                        "--bundle-id",
                        bundle_id,
                        output_path,
                    ]
                )

        other_dylib_paths = [
            os.path.join(volume_path, "usr", "lib"),
            os.path.join(volume_path, "System", "Library", "AccessibilityBundles"),
        ]
        for dylib_path in other_dylib_paths:
            subprocess.check_call(
                [
                    "./symsorter",
                    "-zz",
                    "--ignore-errors",
                    "-o",
                    symcache_output_path,
                    "--prefix",
                    prefix,
                    "--bundle-id",
                    bundle_id,
                    dylib_path,
                ]
            )
    finally:
        logging.info(f"Unmounting {restore_image_path}")
        subprocess.check_call(
            ["hdiutil", "detach", volume_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def extract_ipsw_archive(archive_path: str, extract_dir: str) -> None:
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
    logging.info("Uploading symcache artifacts to production symbols bucket")
    subprocess.check_call(
        ["gsutil", "-m", "cp", "-rn", ".", "gs://sentryio-system-symbols-0"], cwd=symcache_dir
    )


def get_missing_ipsws(os_name: str, os_version: str) -> List[IPSW]:
    if os_name not in DEVICES_TO_CHECK:
        return []

    build_to_ipsw: Dict[str, IPSW] = {}
    for device in DEVICES_TO_CHECK.get(os_name, []):
        res = requests.get(f"https://api.ipsw.me/v2.1/{device.identifier}/{os_version}/info.json")
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
            prefix=os_name,
            architecture=device.architecture,
        )
        if has_symbols_in_cloud_storage(ipsw):
            logging.info(f"We already have symbols for {ipsw.bundle_id}")
            continue

        build_key = f"{os_name}-{latest_os_version}-{latest_build_number}-{device.architecture}"
        if build_to_ipsw.get(build_key):
            continue

        build_to_ipsw[build_key] = ipsw
    return list(build_to_ipsw.values())


def get_latest_version_ipsw(ipsws: List[IPSW]) -> Optional[IPSW]:
    """
    Iterates the list of IPSWs and finds the one with the latest version via
    semantic version comparison.
    """
    if len(ipsws) == 0:
        return None
    latest_version_ipsw = ipsws[0]
    for ipsw in ipsws[1:]:
        if ipsw.build_number > latest_version_ipsw.build_number:
            latest_version_ipsw = ipsw
    return latest_version_ipsw


def has_symbols_in_cloud_storage(ipsw: IPSW) -> bool:
    storage_path = f"gs://sentryio-system-symbols-0/{ipsw.prefix}/bundles/{ipsw.bundle_id}"
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
    main()
