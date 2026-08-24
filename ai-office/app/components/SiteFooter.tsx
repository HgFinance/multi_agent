import { COMPANY } from "../../company.config";

export default function SiteFooter() {
  return (
    <footer className="border-t border-outline-variant bg-surface-container-lowest w-full">
      <div className="max-w-app mx-auto px-margin-mobile md:px-margin-desktop py-4 flex justify-between items-center gap-4 flex-wrap text-label-md font-label-md">
        <b className="text-primary">{COMPANY.name}</b>
        <span className="text-on-surface-variant">
          © {new Date().getFullYear()} {COMPANY.name}. Operational Intelligence Layer.
        </span>
        {/* 아직 화면이 없어 링크로 만들지 않는다 */}
        <span className="flex gap-4 text-on-surface-variant">
          <span>Privacy Policy</span>
          <span>Compliance</span>
          <span>API Docs</span>
        </span>
      </div>
    </footer>
  );
}
