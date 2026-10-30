import type { Metadata } from "next";

export const metadata: Metadata = { title: "Kane System | 任务与成果", description: "Kane System 开发控制面：查看任务、成果与独立验证状态。" };

export default function SystemLayout({ children }: { children: React.ReactNode }) {
  return children;
}
