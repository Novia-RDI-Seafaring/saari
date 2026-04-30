import { useEffect, useRef, useState } from "react";
import { Send, Sparkles } from "lucide-react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

interface Msg {
  role: "user" | "assistant";
  content: string;
}

export function ChatPanel() {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [msgs]);

  async function send() {
    const prompt = draft.trim();
    if (!prompt || streaming) return;
    setDraft("");
    const userMsg: Msg = { role: "user", content: prompt };
    setMsgs((m) => [...m, userMsg, { role: "assistant", content: "" }]);
    setStreaming(true);

    const es = new EventSource(`/api/agent/stream?prompt=${encodeURIComponent(prompt)}`);
    es.addEventListener("message", (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.content) {
          setMsgs((m) => {
            const last = m[m.length - 1];
            if (last.role !== "assistant") return m;
            return [...m.slice(0, -1), { ...last, content: last.content + data.content }];
          });
        }
      } catch {}
    });
    es.addEventListener("delta", (e) => {
      try {
        const { delta } = JSON.parse((e as MessageEvent).data);
        setMsgs((m) => {
          const last = m[m.length - 1];
          if (last.role !== "assistant") return m;
          return [...m.slice(0, -1), { ...last, content: last.content + delta }];
        });
      } catch {}
    });
    es.addEventListener("error", () => {
      setStreaming(false);
      es.close();
    });
    es.addEventListener("done", () => {
      setStreaming(false);
      es.close();
    });
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-4 right-4 flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 shadow-lg hover:bg-zinc-800"
      >
        <Sparkles className="size-4 text-emerald-400" />
        agent
      </button>
    );
  }

  return (
    <aside className="flex h-full w-80 flex-col border-l border-zinc-800 bg-zinc-950 shrink-0">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-emerald-400" />
          <span className="text-sm font-medium">agent</span>
        </div>
        <button
          onClick={() => setOpen(false)}
          className="text-xs text-zinc-500 hover:text-zinc-200"
        >
          hide
        </button>
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3">
        {msgs.length === 0 && (
          <div className="text-xs text-zinc-500 leading-relaxed">
            Ask the agent for fuzzy work — search, snowball, summarize a cluster.
            Precise actions (include/exclude, run search) are faster via direct
            buttons.
          </div>
        )}
        {msgs.map((m, i) => (
          <div
            key={i}
            className={`text-sm whitespace-pre-wrap leading-relaxed ${
              m.role === "user" ? "text-zinc-100" : "text-zinc-300"
            }`}
          >
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-0.5">
              {m.role}
            </div>
            {m.content || (streaming && i === msgs.length - 1 ? "…" : "")}
          </div>
        ))}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
        className="flex gap-1.5 border-t border-zinc-800 p-2"
      >
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="ask the agent…"
          disabled={streaming}
        />
        <Button type="submit" size="icon" disabled={streaming || !draft.trim()}>
          <Send className="size-4" />
        </Button>
      </form>
    </aside>
  );
}
