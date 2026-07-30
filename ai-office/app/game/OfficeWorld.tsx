"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Agent, Company, DeptStatus, Snapshot } from "./sim";
import {
  CEO_ROOM,
  ELEVATOR_SIZE,
  FLOOR_NAME,
  FLOORS,
  MEETING_ROOM,
  PROPS,
  ROOMS,
  TILE,
  WORLD_H,
  WORLD_W,
  floorBand,
  floorOfY,
  elevatorAt,
  roomOf,
  type Floor,
} from "./world";

/** 카메라가 층 범위 밖으로 나갈 수 있는 여유(px). 세로 이동이 답답하지 않을 만큼만. */
const CAM_SLACK_Y = 10 * TILE;

const STATUS_CLASS: Record<DeptStatus, string> = {
  "완료": "done",
  "진행 중": "working",
  "승인 대기": "approval",
  "연동 대기": "blocked",
  "대기": "waiting",
};

type Props = {
  engine: Company;
  snap: Snapshot;
  selectedId: string | null;
  follow: boolean;
  onSelect: (agent: Agent) => void;
};

type Cam = { x: number; y: number; scale: number };

/** 직원 레이어는 한 번만 렌더하고 이후에는 rAF에서 DOM을 직접 갱신한다 */
const AgentLayer = memo(function AgentLayer({
  agents,
  register,
  onPick,
}: {
  agents: Agent[];
  register: (id: string, el: HTMLDivElement | null) => void;
  onPick: (agent: Agent) => void;
}) {
  return (
    <>
      {agents.map((agent) => (
        <div
          key={agent.id}
          ref={(el) => register(agent.id, el)}
          onPointerUp={() => onPick(agent)}
          style={
            {
              "--hair": agent.hair,
              "--shirt": agent.shirt,
              "--accent": agent.accent,
              "--skin": agent.skin,
            } as React.CSSProperties
          }
        >
          <span className="ag-bubble" />
          <span className="ag-bar">
            <i />
          </span>
          <span className="ag-body">
            <i className="p-shadow" />
            <i className="p-leg l" />
            <i className="p-leg r" />
            <i className="p-torso" />
            <i className="p-arm l" />
            <i className="p-arm r" />
            <i className="p-head">
              <b className="p-eye l" />
              <b className="p-eye r" />
            </i>
            <i className="p-hair" />
          </span>
          <span className="ag-tag">
            {agent.name}
            {agent.rank === "lead" ? <em>팀장</em> : null}
            {agent.rank === "ceo" ? <em>대표</em> : null}
          </span>
        </div>
      ))}
    </>
  );
});

const PropLayer = memo(function PropLayer({ floor }: { floor: Floor }) {
  return (
    <>
      {PROPS.filter((prop) => floorOfY(prop.y) === floor).map((prop, i) => (
        <div
          key={i}
          className={`pr pr-${prop.kind}`}
          style={{
            left: prop.x * TILE,
            top: prop.y * TILE,
            width: prop.w * TILE,
            height: prop.h * TILE,
          }}
        >
          {prop.kind === "desk" ? <i className="pr-monitor" /> : null}
          {prop.label ? <span>{prop.label}</span> : null}
        </div>
      ))}
      <div
        className="elevator-mat"
        style={{
          left: (elevatorAt(floor).x - 2) * TILE,
          top: elevatorAt(floor).y * TILE,
          width: ELEVATOR_SIZE.w * TILE,
          height: ELEVATOR_SIZE.h * TILE,
        }}
      >
        🛗 ELEVATOR
      </div>
    </>
  );
});

export default function OfficeWorld({ engine, snap, selectedId, follow, onSelect }: Props) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const agentRefs = useRef(new Map<string, HTMLDivElement>());
  const camRef = useRef<Cam>({ x: WORLD_W / 2, y: WORLD_H / 2, scale: 0.5 });
  const targetRef = useRef<Cam>({ x: WORLD_W / 2, y: WORLD_H / 2, scale: 0.5 });
  const selectedRef = useRef<string | null>(selectedId);
  const dragRef = useRef({ on: false, px: 0, py: 0, moved: false });
  const [zoom, setZoom] = useState<"fit" | "close">("fit");
  const [floor, setFloor] = useState<Floor>(1);

  useEffect(() => {
    selectedRef.current = selectedId;
  }, [selectedId]);

  const hotRoom = useMemo(() => {
    if (snap.spotlight) return snap.spotlight; // 대표 지시로 지목된 방 우선
    if (snap.meetingTitle) return MEETING_ROOM.id;
    if (snap.phaseIndex >= 11) return CEO_ROOM.id;
    const working = Object.entries(snap.deptStatus).find(([, status]) => status === "진행 중");
    return working?.[0] ?? null;
  }, [snap.spotlight, snap.meetingTitle, snap.phaseIndex, snap.deptStatus]);

  /** 카메라가 비출 지점 — 회의실 > 대표실 > 작업 중인 부서 > 출근 시 입구 */
  const focus = useMemo(() => {
    if (hotRoom) {
      const room = roomOf(hotRoom);
      if (room.floor === floor) {
        return { x: (room.x + room.w / 2) * TILE, y: (room.y + room.h / 2) * TILE };
      }
    }
    if (snap.phaseIndex <= 1) return { x: elevatorAt(floor).x * TILE, y: elevatorAt(floor).y * TILE };
    return null;
  }, [hotRoom, snap.phaseIndex, floor]);

  const register = useCallback((id: string, el: HTMLDivElement | null) => {
    if (el) agentRefs.current.set(id, el);
    else agentRefs.current.delete(id);
  }, []);

  const onPick = useCallback(
    (agent: Agent) => {
      if (!dragRef.current.moved) onSelect(agent);
    },
    [onSelect],
  );

  // 카메라 목표
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const compute = () => {
      const rect = viewport.getBoundingClientRect();
      // 층이 실제로 쓰는 영역에만 화면을 배분한다. 빈 띠는 계산에 넣지 않는다.
      const band = floorBand(floor);
      const bandMid = band.top + band.height / 2;
      const fit = Math.min(rect.width / WORLD_W, rect.height / band.height);
      if (zoom === "fit") {
        targetRef.current = { x: WORLD_W / 2, y: bandMid, scale: fit };
        return;
      }
      const scale = Math.max(fit * 1.9, 0.95);
      // 따라가기 대상이 없으면 층 한가운데를 확대한다. 이전 좌표를 유지하면
      // 층을 바꾼 뒤 엉뚱한 빈 곳이 확대돼 보인다.
      targetRef.current = follow && focus ? { ...focus, scale } : { x: WORLD_W / 2, y: bandMid, scale };
    };

    compute();
    const observer = new ResizeObserver(compute);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [zoom, follow, focus, floor]);

  const floorRef = useRef(floor);
  useEffect(() => {
    floorRef.current = floor;
  }, [floor]);

  // 페인트 루프
  useEffect(() => {
    let raf = 0;
    const paint = () => {
      const viewport = viewportRef.current;
      const stage = stageRef.current;
      if (viewport && stage) {
        const cam = camRef.current;
        const target = targetRef.current;
        cam.x += (target.x - cam.x) * 0.07;
        cam.y += (target.y - cam.y) * 0.07;
        cam.scale += (target.scale - cam.scale) * 0.08;

        const rect = viewport.getBoundingClientRect();
        // 층 밖은 렌더되지 않아 빈 공간으로 보인다. 다만 완전히 가두면 답답해서
        // 위아래로 SLACK 만큼은 넘어갈 수 있게 둔다.
        const band = floorBand(floorRef.current);
        const halfH = rect.height / 2 / (cam.scale || 1);
        const lo = band.top + halfH - CAM_SLACK_Y;
        const hi = band.top + band.height - halfH + CAM_SLACK_Y;
        cam.y = lo <= hi ? Math.min(Math.max(cam.y, lo), hi) : band.top + band.height / 2;

        const ox = rect.width / 2 - cam.x * cam.scale;
        const oy = rect.height / 2 - cam.y * cam.scale;
        stage.style.transform = `translate3d(${ox}px, ${oy}px, 0) scale(${cam.scale})`;
        if (stage.classList.contains("compact") !== cam.scale < 0.62) {
          stage.classList.toggle("compact", cam.scale < 0.62);
        }

        const picked = selectedRef.current;
        for (const agent of engine.agents) {
          const el = agentRefs.current.get(agent.id);
          if (!el) continue;

          el.style.transform = `translate3d(${(agent.x + 0.5 + agent.jitter) * TILE}px, ${
            (agent.y + 0.9) * TILE
          }px, 0)`;
          el.style.zIndex = String(200 + Math.round(agent.y));

          const cls =
            `ag f-${agent.facing} a-${agent.anim} r-${agent.rank}` +
            (agent.id === picked ? " selected" : "") +
            (agent.status === "출근 전" ? " offstage" : "");
          if (el.className !== cls) el.className = cls;

          const bubble = el.firstElementChild as HTMLElement;
          const text = agent.speech ?? "";
          if (bubble.dataset.text !== text) {
            bubble.dataset.text = text;
            bubble.textContent = text;
            bubble.className = `ag-bubble ${agent.speechKind}${text ? " on" : ""}`;
          }

          const bar = el.children[1] as HTMLElement;
          const fill = bar.firstElementChild as HTMLElement;
          const show = agent.anim === "type" ? "1" : "0";
          if (bar.style.opacity !== show) bar.style.opacity = show;
          if (show === "1") fill.style.width = `${Math.round(agent.progress * 100)}%`;
        }
      }
      raf = requestAnimationFrame(paint);
    };
    raf = requestAnimationFrame(paint);
    return () => cancelAnimationFrame(raf);
  }, [engine]);

  // 포인터 캡처는 쓰지 않는다 — 캡처하면 HUD 버튼·직원 클릭이 뷰포트에 먹혀버린다
  const onPointerDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest(".world-hud")) return;
    dragRef.current = { on: true, px: e.clientX, py: e.clientY, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag.on) return;
    const dx = e.clientX - drag.px;
    const dy = e.clientY - drag.py;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    drag.px = e.clientX;
    drag.py = e.clientY;
    const scale = camRef.current.scale || 1;
    targetRef.current = {
      ...targetRef.current,
      x: clamp(targetRef.current.x - dx / scale, 0, WORLD_W),
      y: clamp(targetRef.current.y - dy / scale, 0, WORLD_H),
    };
  };
  const onPointerUp = () => {
    dragRef.current.on = false;
    window.setTimeout(() => {
      dragRef.current.moved = false;
    }, 0);
  };

  return (
    <div className="world-frame">
      <div
        className="world-viewport"
        ref={viewportRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={onPointerUp}
      >
        <div className="world-stage" ref={stageRef} style={{ width: WORLD_W, height: WORLD_H }}>
          <div className="world-floor" />

          {ROOMS.filter((room) => room.floor === floor).map((room) => {
            const status = snap.deptStatus[room.id];
            return (
              <div
                key={room.id}
                className={`rm rm-${room.kind} ${status ? STATUS_CLASS[status] : ""} ${
                  hotRoom === room.id ? "hot" : ""
                }`}
                style={{
                  left: room.x * TILE,
                  top: room.y * TILE,
                  width: room.w * TILE,
                  height: room.h * TILE,
                }}
              >
                <span className="rm-head">
                  <b>
                    {room.icon} {room.name}
                  </b>
                  {status ? <i className={`rm-dot ${STATUS_CLASS[status]}`} title={status} /> : null}
                </span>
              </div>
            );
          })}

          <PropLayer floor={floor} />
          <AgentLayer
            agents={engine.agents.filter((agent) => floorOfY(agent.y) === floor)}
            register={register}
            onPick={onPick}
          />
        </div>

        <div className="world-hud">
          <button className={zoom === "fit" ? "on" : ""} onClick={() => setZoom("fit")}>
            🗺️ 전체 보기
          </button>
          <button className={zoom === "close" ? "on" : ""} onClick={() => setZoom("close")}>
            🔍 가까이
          </button>
          {FLOORS.map((value) => (
            <button
              key={value}
              className={floor === value ? "on" : ""}
              onClick={() => setFloor(value)}
              title={FLOOR_NAME[value]}
            >
              {value === 1 ? "🏢 1층" : "🏢 2층"}
            </button>
          ))}
        </div>
        <div className="world-hint">드래그로 둘러보기 · 직원 클릭하면 프로필</div>
      </div>
    </div>
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}
