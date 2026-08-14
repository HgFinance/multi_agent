/** Dashboard 패널 공통 헤더. `DashboardView.tsx`와 `CeoControlRoomChat.tsx`가 함께 쓴다. */
export function PanelBar({
  icon,
  title,
  children,
}: {
  icon: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="bg-surface-container-low border-b border-outline-variant px-4 py-2.5 flex items-center justify-between gap-2">
      <span className="flex items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{icon}</span>
        {title}
      </span>
      {children}
    </div>
  );
}
