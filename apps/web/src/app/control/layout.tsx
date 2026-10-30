import type { Metadata } from "next";
import "./control.css";
export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Kane System | 工作区",
  description: "Owner 控制、原生文档与人机接管",
  manifest: "/control/manifest.webmanifest",
  appleWebApp: { capable: true, title: "Kane System" },
};
export default function ControlLayout({ children }: { children: React.ReactNode }) {
  return children;
}
