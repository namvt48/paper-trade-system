export default function Loading() {
  return (
    <div className="space-y-8">
      <div>
        <div className="h-4 w-24 bg-slate-800 rounded animate-pulse mb-2" />
        <div className="h-7 w-48 bg-slate-800 rounded animate-pulse mb-3" />
        <div className="flex gap-3">
          <div className="h-6 w-20 bg-slate-800 rounded-full animate-pulse" />
          <div className="h-5 w-40 bg-slate-800 rounded animate-pulse" />
        </div>
      </div>

      <div>
        <div className="h-4 w-28 bg-slate-800 rounded animate-pulse mb-3" />
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3">
              <div className="h-3 w-16 bg-slate-700/60 rounded animate-pulse mb-2" />
              <div className="h-5 w-20 bg-slate-700/40 rounded animate-pulse" />
            </div>
          ))}
        </div>
      </div>

      <div>
        <div className="h-4 w-24 bg-slate-800 rounded animate-pulse mb-3" />
        <div className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-4 h-48">
          <div className="h-full w-full bg-slate-700/30 rounded animate-pulse" />
        </div>
      </div>

      <div>
        <div className="h-4 w-32 bg-slate-800 rounded animate-pulse mb-3" />
        <div className="rounded-xl border border-slate-700/60 overflow-hidden">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="border-b border-slate-700/40 px-4 py-3 flex gap-4">
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
