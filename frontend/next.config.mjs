/** @type {import('next').NextConfig} */
const nextConfig = {
  // Don't advertise the framework version in the X-Powered-By header.
  poweredByHeader: false,

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          // Clickjacking defense: the console is a single-origin app.
          { key: "X-Frame-Options", value: "DENY" },
          // MIME-sniffing defense.
          { key: "X-Content-Type-Options", value: "nosniff" },
          // Referrer leakage: never send the origin to other sites.
          { key: "Referrer-Policy", value: "same-origin" },
          // Lock down powerful browser features the UI never uses.
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
    ];
  },
  // NOTE: no `output: "standalone"` — the frontend is not containerized
  // (there is no frontend Dockerfile); add it if that changes.
};

export default nextConfig;
