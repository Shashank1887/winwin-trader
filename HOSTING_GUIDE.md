# Free Personal Hosting Guide (Use From Anywhere)

## Option 1: 100% Private & Zero-Sleep (Tailscale — Recommended for Personal Use)
Best if your PC can stay on during trading hours (9:20 AM – 11:30 AM IST). Completely private—only your own devices can access it.
1. Install **Tailscale** (Free Personal Plan) on:
   - Your Windows PC
   - Your **iPhone 14 Plus** (App Store)
   - Your **OnePlus Pad Go** (Play Store)
2. Sign in with the same Google/Apple account on all 3 devices.
3. On your PC, start the app (`python api_server.py` for Mobile Web port `8000`, or `python -m streamlit run app.py --server.address 0.0.0.0` for port `8501`).
4. Check your PC's Tailscale IP (e.g., `100.x.y.z`) and open from anywhere on 4G/5G:
   - Mobile Web App: `http://100.x.y.z:8000`
   - Streamlit App: `http://100.x.y.z:8501`

---

## Option 2: Free Public HTTPS Link From Your PC (Cloudflare Tunnel)
No Tailscale app needed on phone; protected by your `MOBILE_API_TOKEN` from `.env`.
1. Install Cloudflare Tunnel once:
   ```powershell
   winget install Cloudflare.cloudflared
   ```
2. Keep `python api_server.py` running, then open a second terminal:
   ```powershell
   cloudflared tunnel --url http://localhost:8000
   ```
3. Open the `https://....trycloudflare.com` URL on your iPhone 14 Plus or OnePlus Pad Go anywhere over 4G/5G.

---

## Option 3: Free 24/7 Cloud Hosting (PC Can Stay OFF)

> **Security Warning:** Keep `.env` private (it is already in `.gitignore`). Never upload `.env` to GitHub—enter `MOBILE_API_TOKEN` and Angel One keys only in your cloud host's **Environment Variables / Secrets** dashboard.

### A. Render.com (Free Docker Web Service)
1. Push this project to a **Private GitHub repository** (verify `.env` and `trades_log.csv` are NOT pushed).
2. On [render.com](https://render.com) → **New → Web Service** → connect your private repo.
3. Choose **Environment: Docker** and **Instance Type: Free**.
4. Add **Environment Variables**:
   - `MOBILE_API_TOKEN` = *(copy from your `.env`)*
   - *(Optional for Live Trading)* `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`
5. Deploy! Open `https://<your-service>.onrender.com` on your iPhone 14 Plus / OnePlus Pad Go.
   - *Note:* Render's free tier sleeps after 15 minutes of inactivity. Use a free monitor (e.g., UptimeRobot pinging `/health` every 5 min) if you want Auto-Pilot awake continuously.

### B. Hugging Face Spaces (Free Private Docker Space)
1. Create a **Private Space** on [huggingface.co/spaces](https://huggingface.co/spaces) with **Docker** SDK.
2. Add a Space `README.md` header with `app_port: 7860` and set `PORT=7860` + `MOBILE_API_TOKEN` in **Space Settings → Secrets**.
3. Push the code (`Dockerfile` automatically binds `${PORT:-8000}`).
