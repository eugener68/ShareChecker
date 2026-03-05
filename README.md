# ShareChecker

Small Windows desktop app (Tkinter) that shows a share card for a ticker symbol.

Default symbol at startup: `DOX`.

## Features
- Opening price
- Close price
  - If NYSE is still open, card uses previous day close
- Daily change (%)
- Daily change ($)
- Interactive symbol input (`Load` button + Enter key)
- Real-time symbol validation against online US symbol lists
- Prefix-based autocomplete suggestions while typing (scrollable)

## Requirements
- Windows or macOS
- Python 3.11+ (Python 3.12 recommended for packaging)

## Setup
1. Create and activate a virtual environment.
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

## Run
```powershell
python app.py
```

## Packaging (Standalone Apps)
Packaging tools are more stable on Python 3.12. It is recommended to build
distributables using a 3.12 virtual environment.

### macOS (.app with py2app)
```bash
python -m venv .venv312
source .venv312/bin/activate
pip install -r requirements.txt -r requirements-packaging.txt
python setup.py py2app
```
Output: `dist/ShareChecker.app`

Optional notarization (Developer ID):
```bash
# One-time setup
xcrun notarytool store-credentials "notary-profile" \
  --apple-id "your@appleid.com" \
  --team-id "HBY3U59ZBD" \
  --password "app-specific-password"

# Build + notarize
./scripts/build_macos.sh --notarize
```
To use a different keychain profile:
```bash
NOTARY_PROFILE="your-profile" ./scripts/build_macos.sh --notarize
```

### Windows (.exe with PyInstaller)
```powershell
py -3.12 -m venv .venv312
.venv312\Scripts\activate
pip install -r requirements.txt -r requirements-packaging.txt
pyinstaller build_windows.spec
```
Output: `dist\ShareChecker.exe`

Optional startup symbol:
```powershell
python app.py --symbol MSFT
```

## Proxy / TLS (corporate network)
Set proxy variables in the same PowerShell session before running:
```powershell
$env:HTTP_PROXY = "<YOUR_PROXY>"
$env:HTTPS_PROXY = "<YOUR_PROXY>"
python app.py
```

If TLS inspection is enabled, set your corporate CA bundle:
```powershell
$env:REQUESTS_CA_BUNDLE = "<CA_PATH>"
python app.py
```

Or pass CA bundle directly:
```powershell
python app.py --ca-bundle "<CA_PATH>"
```

Temporary troubleshooting only (not recommended):
```powershell
python app.py --insecure-ssl
```
