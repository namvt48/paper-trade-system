export default function Loading() {
  return (
    <div className="space-y-8">
      <div>
        <div className="h-3 w-20 bg-slate-800 rounded animate-pulse mb-2" />
        <div className="h-7 w-40 bg-slate-800 rounded animate-pulse" />
      </div>

      <div className="rounded-xl border border-slate-700/60 overflow-hidden">
        <div className="bg-slate-800/80 border-b border-slate-700/60 px-4 py-3 flex gap-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-3 bg-slate-700/60 rounded animate-pulse flex-1" />
          ))}
        </div>
        <div>
          {Array.from({ length: 8 }).map((_, i) => (
            <div
              key={i}
              className={`border-b border-slate-700/40 px-4 py-3 flex gap-4 ${i % 2 === 0 ? "" : "bg-slate-800/30"}`}
            >
              {Array.from({ length: 8 }).map((_, j) => (
                <div key={j} className="h-4 bg-slate-700/40 rounded animate-pulse flex-1" />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
