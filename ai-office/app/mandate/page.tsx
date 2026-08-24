import type { Metadata } from "next";
import TopNav from "../components/TopNav";
import SiteFooter from "../components/SiteFooter";
import MandateConfig from "./MandateConfig";

export const metadata: Metadata = {
  title: "Mandate Configuration - Sentient Capital",
};

export default function MandatePage() {
  return (
    <div className="bg-background text-on-background min-h-screen flex flex-col font-sans">
      <TopNav current="mandate" />
      <MandateConfig />
      <SiteFooter />
    </div>
  );
}
