import type { Metadata } from "next";
import "./globals.css";
import "./office.css";
import { COMPANY } from "../company.config";

export const metadata: Metadata = {
  title: COMPANY.pageTitle,
  description: COMPANY.description,
  openGraph: {
    title: COMPANY.name,
    description: "AI가 일하고, 대표가 결정하는 회사",
    images: [{ url: "/og.png", width: 1672, height: 935, alt: `${COMPANY.name} 픽셀 오피스` }],
  },
  twitter: {
    card: "summary_large_image",
    title: COMPANY.name,
    description: "AI가 일하고, 대표가 결정하는 회사",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body>{children}</body>
    </html>
  );
}
