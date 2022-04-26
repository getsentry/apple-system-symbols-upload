# Apple system symbols upload scripts

These scripts are meant to extract and upload to `gs://sentryio-system-symbols-0` Apple system symbols for `iOS`, `tvOS` and `macOS`. Simulators are also supported and simulators listed on https://github.com/actions/virtual-environments/blob/main/images/macos/macos-12-Readme.md#installed-simulators are supported.

A GitHub action will run every 4 hours to check for new versions of supported OSes.

Each script checks on the bucket if the bundle already exists before extracting and uploading symbols. See the `bundles` directory inside each OS directory on the bucket.

## Set up environment

Install `gsutil` and configure access to the bucket.
```sh
        brew install --cask google-cloud-sdk
        echo "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin" >> $GITHUB_PATH
```
You need access to `gs://sentryio-system-symbols-0`. The key is in the OSS vault in 1Password.
```sh
gcloud auth activate-service-account --key-file="$key_path"
```
Install some binary dependencies to unzip the IPSW archives and extract the symbols.
```sh
brew install p7zip keith/formulae/dyld-shared-cache-extractor
```
You will need `symsorter` to extract the symcache and organize it for the bucket.
```sh
git clone https://github.com/getsentry/symbolicator.git
pushd symbolicator/crates/symsorter
cargo build --release
popd
cp ./symbolicator/target/release/symsorter .
```
You also need to install `Xcode` and configure it properly.
```sh
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```
Finally, you'll need Python >=3.8 and some packages to run the scripts.
```sh
python3 -m pip install --user -r requirements.txt
```

## Usage
### Upload simulators
You need to specify which os you want to extract the symbols for and it will target the latest version.
```python
python3 import_system_symbols_from_ipsw.py --os_name tvos
```
You can also specify a specific version of the OS.
```python
python3 import_system_symbols_from_ipsw.py --os_name tvos --os_version 15.3
```
The script to extract from simulators doesn't take any argument.
```python
 python3 import_system_symbols_from_simulators.py
```
