# Installing the CA Certificate on Other Devices

These instructions are run on the **client device** (the machine or phone you browse from), not the server. First, get the certificate onto your device. The simplest way is to open `https://<server-ip>:3000/api/cert` in the device's browser (tap through the certificate warning once). It serves the mkcert root CA when mkcert is set up on the server (the in-app **Regenerate Certificate** step signs with mkcert), otherwise the installer's self-signed certificate. From a computer you can also copy it from the server:

```bash
scp user@server-ip:~/xylocopa-main/certs/selfsigned.crt ~/xylocopa.crt
```

If the certificate was regenerated with mkcert, copy the mkcert root instead: `~/.local/share/mkcert/rootCA.pem` on a Linux server, `~/Library/Application Support/mkcert/rootCA.pem` on a macOS server.

The commands below call the file `xylocopa.crt`. A download from `/api/cert` is named `xylocopa-ca.crt` (mkcert root) or `xylocopa.crt`, and a copied mkcert root is `rootCA.pem`, so rename it or adjust the path. On Linux the `.crt` extension is required.

> **Tip:** If you're unsure how to install certificates on your OS, paste the relevant section below into Claude or another AI assistant, it can walk you through the steps interactively.

> **Migrating from AgentHive?** Existing `agenthive.crt` / `agenthive-ca.crt` installations remain trusted, no action needed. Newly issued certs use the `xylocopa.crt` filename.

## iPhone / iPad

1. In **Safari**, open `https://<server-ip>:3000/api/cert` and allow the profile download.
2. **Settings > General > VPN & Device Management**, tap the downloaded profile, then **Install**.
3. **Settings > General > About > Certificate Trust Settings**: under *Enable full trust for root certificates*, turn on the certificate ("mkcert …" or "xylocopa") and tap **Continue**.

Step 3 is required. Installing the profile does not make iOS trust it for HTTPS. Without the switch, the app works only while Safari's temporary "visit this website" exception lasts. When iOS drops that exception, every connection is refused and the app stays on "Reconnecting…" ("connection failed (Load failed)") until the exception is granted again.

## Android

1. Transfer the certificate to the device (downloaded from `/api/cert` or copied above)
2. **Settings > Security > Encryption & credentials > Install a certificate > CA certificate**
3. Select the file and confirm

## macOS

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain xylocopa.crt
```

## Windows

```powershell
certutil -addstore "Root" xylocopa.crt
```

## Linux

```bash
sudo cp xylocopa.crt /usr/local/share/ca-certificates/xylocopa.crt
sudo update-ca-certificates
```

---

Restart your browser after installing.
