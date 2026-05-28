"use client";

import { useEffect, useState } from "react";

export function UTCClock() {
  const [time, setTime] = useState("");

  useEffect(() => {
    function tick() {
      const now = new Date();
      const hh = String(now.getUTCHours()).padStart(2, "0");
      const mm = String(now.getUTCMinutes()).padStart(2, "0");
      const ss = String(now.getUTCSeconds()).padStart(2, "0");
      setTime(`${hh}:${mm}:${ss}`);
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex items-center gap-2 font-mono text-sm">
      <span className="text-slate-500 text-xs tracking-widest uppercase">UTC</span>
      <span className="text-slate-200 tabular-nums">{time || "00:00:00"}</span>
    </div>
  );
}
