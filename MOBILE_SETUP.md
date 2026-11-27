# Mobile App Setup (iPhone 14 Plus & OnePlus Pad Go)

This workspace now includes:
- **`api_server.py`**: Authenticated FastAPI HTTPS backend sharing `risk_manager.py`, `strategy.py`, and `broker.py`.
- **`mobile/`**: Native React Native (Expo SDK 58, TypeScript) app responsive for **iPhone 14 Plus (iOS)** and **OnePlus Pad Go (Android Tablet)**.

## Step 1: Start the Backend & Get HTTPS Access (for 4G/5G)

### Option A — Quick HTTPS Tunnel from Your Windows PC
1. Start the API server:
   ```powershell
   python api_server.py
   ```
   Copy the printed `MOBILE_API_TOKEN` (also saved in `.env`).
2. Expose port `8000` over HTTPS using Cloudflare Tunnel (`winget install Cloudflare.cloudflared`):
   ```powershell
   cloudflared tunnel --url http://localhost:8000
   ```
   Copy the `https://....trycloudflare.com` URL.

### Option B — Always-On Cloud Hosting (Render / Railway / Fly.io)
Deploy this repository using the included `Dockerfile`:
- Set environment variables: `MOBILE_API_TOKEN=<your-secret-token>` (plus optional `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`).
- Expose port `8000`.

## Step 2: Run on iPhone 14 Plus & OnePlus Pad Go

### Instant Run Today (No Mac Required)
1. Install **Expo Go** from the Apple App Store (iPhone 14 Plus) and Google Play Store (OnePlus Pad Go).
2. In a second terminal on your PC:
   ```powershell
   cd mobile
   npx expo start --tunnel
   ```
3. Scan the QR code with your iPhone 14 Plus Camera or OnePlus Pad Go Expo Go app.
4. Open the **Settings** tab in the app, paste your **Backend HTTPS URL** and **MOBILE_API_TOKEN**, and tap **Save & Connect Server**.

### Standalone `.ipa` (iPhone) & `.apk` (Android) Builds via EAS Cloud
Because compiling a native iOS `.ipa` requires macOS/Xcode locally, you can build both binaries in the cloud directly from Windows using Expo EAS:
```powershell
cd mobile
npm install -g eas-cli
eas login
eas build --platform android --profile preview   # Downloads .apk for OnePlus Pad Go
eas build --platform ios                           # Cloud-builds iOS binary (requires Apple Developer account)
```
