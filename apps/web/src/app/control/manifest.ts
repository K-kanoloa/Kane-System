import type { MetadataRoute } from "next";
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Kane System", short_name: "Kane System", lang: "zh-CN",
    description: "Owner 工作区与人机接管", start_url: "/control", scope: "/control",
    display: "standalone", background_color: "#f4f6f5", theme_color: "#293e34",
    icons: [{ src: "/kane-control-icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" }],
  };
}
