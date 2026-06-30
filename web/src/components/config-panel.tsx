"use client";

import { useState } from "react";

interface ConfigPanelProps {
  config: Record<string, unknown>;
}

function formatConfigValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

export function ConfigPanel({ config }: ConfigPanelProps) {
  const [isOpen, setIsOpen] = useState(false);
  const configEntries = Object.entries(config);

  return (
    <div>
      <h2
        onClick={() => setIsOpen(!isOpen)}
        className="text-sm font-semibold mb-3 text-slate-300 uppercase tracking-widest flex items-center gap-2 cursor-pointer select-none group hover:text-white transition-colors"
      >
        <span className="w-3 h-px bg-slate-700 inline-block" />
        <span>Config</span>
        <span className="text-[10px] lowercase text-slate-500 group-hover:text-indigo-400 font-normal transition-colors ml-1">
          {isOpen ? "(click to hide)" : "(click to show)"}
        </span>
        <span className="text-slate-500 group-hover:text-indigo-400 text-xs ml-auto transition-colors font-mono">
          {isOpen ? "▲" : "▼"}
        </span>
      </h2>

      <div className={`transition-all duration-200 overflow-hidden ${
        isOpen ? "max-h-[2000px] opacity-100 mt-2" : "max-h-0 opacity-0 pointer-events-none"
      }`}>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 pb-2">
          {configEntries.length > 0 ? (
            configEntries.map(([key, value]) => (
              <div key={key} className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3 hover:border-slate-600/60 transition-colors">
                <div className="text-slate-500 text-xs mb-1 uppercase tracking-wide">{key}</div>
                <div className="font-mono text-sm text-slate-200 break-words">{formatConfigValue(value)}</div>
              </div>
            ))
          ) : (
            <div className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3 text-sm text-slate-500 col-span-full">
              No config registered for this alpha.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
