import type { Metadata } from "next";
import TopNav from "../components/TopNav";
import SiteFooter from "../components/SiteFooter";
import AgentLogsView from "./AgentLogsView";

export const metadata: Metadata = {
  title: "Agent Logs - HgFinance",
};

export default function AgentLogsPage() {
  return (
    <div className="bg-background text-on-background min-h-screen flex flex-col font-sans">
      <TopNav current="agent-logs" />
      <AgentLogsView />
      <SiteFooter />
    </div>
  );
}
