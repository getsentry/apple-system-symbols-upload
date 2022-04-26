#!/usr/bin/env python3

import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List

from import_system_symbols_from_ipsw import has_symbols_in_cloud_storage, symsorter, upload_to_gcs


@dataclass
class SimulatorRuntime:
    arch: str
    build_number: str
    macos_version: str
    os_name: str
    os_version: str
    path: str

    @property
    def bundle_id(self) -> str:
        return f"simulator_{self.macos_version}_{self.os_version}_{self.build_number}_{self.arch}"


_simulator_runtime_prefix = "com.apple.CoreSimulator.SimRuntime."
_dyld_shared_cache_prefix = "dyld_sim_shared_cache_"


def main():
    logging.basicConfig(level=logging.INFO, format="[sentry] %(message)s")
    caches_path = os.path.expanduser("~/Library/Developer/CoreSimulator/Caches/dyld")
    if not os.path.isdir(caches_path):
        sys.exit(f"{caches_path} does not exist")
    with tempfile.TemporaryDirectory(prefix="_sentry_dyld_shared_cache_") as output_dir:
        for runtime in find_simulator_runtimes(caches_path):
            map_paths: List[str] = []
            for filename in os.listdir(runtime.path):
                if not filename.startswith(_dyld_shared_cache_prefix):
                    continue
                full_path = os.path.join(runtime.path, filename)
                if os.path.splitext(filename)[1] == ".map":
                    map_paths.append(full_path)
                    continue
                runtime.arch = filename.split(_dyld_shared_cache_prefix)[1]
                if has_symbols_in_cloud_storage(runtime.os_name, runtime.bundle_id):
                    logging.info(
                        f"Already have symbols for macOS {runtime.macos_version}, {runtime.os_name} {runtime.os_version} {runtime.arch}, skipping"
                    )
                    continue
                logging.info(
                    f"Extracting symbols for macOS {runtime.macos_version}, {runtime.os_name} {runtime.os_version} {runtime.arch}"
                )
                extract_system_symbols(runtime, output_dir)
        upload_to_gcs(output_dir)


def find_simulator_runtimes(caches_path: str) -> List[SimulatorRuntime]:
    runtimes: List[SimulatorRuntime] = []
    for macos_version in os.listdir(caches_path):
        if macos_version == ".DS_Store":
            continue
        for simruntime_name in os.listdir(os.path.join(caches_path, macos_version)):
            if not simruntime_name.startswith(_simulator_runtime_prefix):
                continue
            splits = simruntime_name.split(".")
            build_number = splits[5]
            os_info = splits[4].split("-")
            os_version = ".".join(os_info[1:3])
            os_name = os_info[0].lower()
            path = os.path.join(caches_path, macos_version, simruntime_name)
            for filename in os.listdir(path):
                if not filename.startswith(_dyld_shared_cache_prefix):
                    continue
                arch = filename.split(_dyld_shared_cache_prefix)[1]
                break
            runtimes.append(
                SimulatorRuntime(
                    arch=arch,
                    build_number=build_number,
                    macos_version=macos_version,
                    os_name=os_name,
                    os_version=os_version,
                    path=path,
                )
            )
    return runtimes


def extract_system_symbols(runtime: SimulatorRuntime, output_dir: str) -> None:
    for filename in os.listdir(runtime.path):
        if not filename.startswith(_dyld_shared_cache_prefix):
            continue
        if os.path.splitext(filename)[1] == ".map":
            continue
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_output") as dsc_out_dir:
            full_path = os.path.join(runtime.path, filename)
            subprocess.check_call(["dyld-shared-cache-extractor", full_path, dsc_out_dir])
            symsorter(output_dir, runtime.os_name, runtime.bundle_id, dsc_out_dir)


if __name__ == "__main__":
    main()
