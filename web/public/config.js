// Development default. In Docker the web container replaces it at start from PUBLIC_BASE_URL
// (web/docker/config.sh), so the join QR code points at the laptop's LAN address, not localhost.
window.CB_CONFIG = { publicBaseUrl: '' }
