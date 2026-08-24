import type { Metadata } from "next";
import TopNav from "../components/TopNav";
import SiteFooter from "../components/SiteFooter";
import DashboardView from "./DashboardView";

export const metadata: Metadata = {
  title: "Dashboard - HgFinance",
};

export default function DashboardPage() {
  return (
    <div className="bg-background text-on-background min-h-screen flex flex-col font-sans">
      <TopNav current="dashboard" />
      <DashboardView />
      <SiteFooter />
    </div>
  );
}
