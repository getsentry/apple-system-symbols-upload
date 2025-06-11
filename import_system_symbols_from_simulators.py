import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List

import sentry_sdk

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
_ignored_dyld_file_suffixes = (".map", ".atlas", ".dylddata")


def _is_ignored_dsc_file(filename: str) -> bool:
    return (
        not filename.startswith(_dyld_shared_cache_prefix)
        or os.path.splitext(filename)[1] in _ignored_dyld_file_suffixes
    )


def retrieve_caches_path() -> str:
    root_caches_path = "/Library/Developer/CoreSimulator/Caches/dyld"
    user_caches_path =  os.path.expanduser(f"~{root_caches_path}")

    # starting with Xcode 16 simulator image caches are stored in the root `Library` folder
    if not os.path.isdir(root_caches_path):
        # up to Xcode 16 simulator image caches were stored per user
        if not os.path.isdir(user_caches_path):
            sys.exit(f"Neither {root_caches_path} nor {user_caches_path} do exist")
        else:
            caches_path = user_caches_path
    else:
        caches_path = root_caches_path

    return caches_path


def main():
    logging.basicConfig(level=logging.INFO, format="[sentry] %(message)s")

    with sentry_sdk.start_transaction(
        op="task", name="import symbols from simulators"
    ) as transaction:
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_shared_cache_") as output_dir:
            for runtime in find_simulator_runtimes(retrieve_caches_path()):
                with transaction.start_child(
                    op="task", description="Process runtime"
                ) as runtime_span:
                    runtime_span.set_data("runtime", runtime)
                    for filename in os.listdir(runtime.path):
                        if _is_ignored_dsc_file(filename):
                            continue

                        with runtime_span.start_child(
                            op="task", description="Process file"
                        ) as file_span:
                            runtime.arch = filename.split(_dyld_shared_cache_prefix)[1]
                            file_span.set_data("file", filename)
                            file_span.set_data("architecture", runtime.arch)
                            with file_span.start_child(
                                op="task", description="Check if version has symbols already"
                            ):
                                if has_symbols_in_cloud_storage(runtime.os_name, runtime.bundle_id):
                                    logging.info(
                                        f"Already have symbols for {runtime.os_name} {runtime.os_version} {runtime.arch} from macOS {runtime.macos_version}, skipping"
                                    )
                                    continue
                            logging.info(
                                f"Extracting symbols for macOS {runtime.macos_version}, {runtime.os_name} {runtime.os_version} {runtime.arch}"
                            )
                            with file_span.start_child(op="task", description="Extract symbols"):
                                extract_system_symbols(runtime, output_dir)
            with transaction.start_child(op="task", description="Upload results to GCS"):
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
                break
    return runtimes


def extract_system_symbols(runtime: SimulatorRuntime, output_dir: str) -> None:
    span = sentry_sdk.get_current_span()
    for filename in os.listdir(runtime.path):
        if _is_ignored_dsc_file(filename):
            continue

        with span.start_child(
            op="task", description="Extract symbols from runtime file"
        ) as file_span:
            file_span.set_data("runtime_file", filename)
            with tempfile.TemporaryDirectory(prefix="_sentry_dyld_output") as dsc_out_dir:
                full_path = os.path.join(runtime.path, filename)
                with file_span.start_child(
                    op="task", description="Run dyld-shared-cache-extractor"
                ):
                    subprocess.check_call(["dyld-shared-cache-extractor", full_path, dsc_out_dir])
                with file_span.start_child(op="task", description="Run symsorter"):
                    symsorter(output_dir, runtime.os_name, runtime.bundle_id, dsc_out_dir)


if __name__ == "__main__":
    sentry_sdk.init(
        dsn="https://f86a0e29c86e49688d691e194c5bf9eb@o1.ingest.sentry.io/6418660",
        traces_sample_rate=1.0,
    )
    main()
