import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "GrantWatch Comments",
  description: "Sample form submitting comments to Neon via server action",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif" }}>{children}</body>
    </html>
  );
}
