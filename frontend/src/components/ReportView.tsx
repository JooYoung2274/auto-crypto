import { useEffect, useState } from "react"
import type { ReactNode } from "react"
import { api } from "../lib/api"
import { reportKindLabel } from "../lib/cycleKind"
import type { Report, ReportSummary } from "../lib/types"

// ---------------------------------------------------------------------------
// Tiny markdown → React renderer (headings, lists, tables, code, quotes, hr,
// bold/italic/inline-code). No dependencies — reports come from our own
// backend generator, so this deliberately covers only what it emits.
// ---------------------------------------------------------------------------

function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith("**")) out.push(<strong key={`${keyBase}-b${k++}`}>{tok.slice(2, -2)}</strong>)
    else if (tok.startsWith("`")) out.push(<code key={`${keyBase}-c${k++}`}>{tok.slice(1, -1)}</code>)
    else out.push(<em key={`${keyBase}-i${k++}`}>{tok.slice(1, -1)}</em>)
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const splitRow = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim())

const isTableDivider = (line: string) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-")

export function renderMarkdown(md: string): ReactNode[] {
  const lines = md.split(/\r?\n/)
  const blocks: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]

    if (line.trim() === "") {
      i++
      continue
    }

    // fenced code block
    if (line.trim().startsWith("```")) {
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        buf.push(lines[i])
        i++
      }
      i++ // closing fence
      blocks.push(
        <pre key={`k${key++}`} className="md-code">
          <code>{buf.join("\n")}</code>
        </pre>,
      )
      continue
    }

    // heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line)
    if (h) {
      const level = h[1].length
      const Tag = `h${Math.min(6, level + 2)}` as "h3" | "h4" | "h5" | "h6"
      blocks.push(
        <Tag key={`k${key++}`} className={`md-h md-h${level}`}>
          {renderInline(h[2], `h${key}`)}
        </Tag>,
      )
      i++
      continue
    }

    // horizontal rule
    if (/^ {0,3}([-*_])\1{2,}\s*$/.test(line)) {
      blocks.push(<hr key={`k${key++}`} className="md-hr" />)
      i++
      continue
    }

    // table
    if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      const header = splitRow(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitRow(lines[i]))
        i++
      }
      blocks.push(
        <div key={`k${key++}`} className="table-scroll">
          <table className="data-table md-table">
            <thead>
              <tr>
                {header.map((c, ci) => (
                  <th key={ci}>{renderInline(c, `th${key}-${ci}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, ci) => (
                    <td key={ci}>{renderInline(c, `td${key}-${ri}-${ci}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // blockquote
    if (/^>\s?/.test(line)) {
      const buf: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""))
        i++
      }
      blocks.push(
        <blockquote key={`k${key++}`} className="md-quote">
          {renderInline(buf.join(" "), `q${key}`)}
        </blockquote>,
      )
      continue
    }

    // list (unordered / ordered)
    const listRe = /^\s*(?:[-*]|\d+\.)\s+/
    if (listRe.test(line)) {
      const ordered = /^\s*\d+\./.test(line)
      const items: string[] = []
      while (i < lines.length && listRe.test(lines[i])) {
        items.push(lines[i].replace(listRe, ""))
        i++
      }
      const children = items.map((it, ii) => <li key={ii}>{renderInline(it, `li${key}-${ii}`)}</li>)
      blocks.push(
        ordered ? (
          <ol key={`k${key++}`} className="md-list">
            {children}
          </ol>
        ) : (
          <ul key={`k${key++}`} className="md-list">
            {children}
          </ul>
        ),
      )
      continue
    }

    // paragraph — greedy until blank line or a structural line
    const buf: string[] = [line]
    i++
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !lines[i].trim().startsWith("```") &&
      !listRe.test(lines[i]) &&
      !/^>\s?/.test(lines[i])
    ) {
      buf.push(lines[i])
      i++
    }
    blocks.push(
      <p key={`k${key++}`} className="md-p">
        {renderInline(buf.join(" "), `p${key}`)}
      </p>,
    )
  }

  return blocks
}

// ---------------------------------------------------------------------------

/**
 * reports.created_at comes from SQLite datetime('now') — UTC but with NO
 * timezone suffix, so tag it as UTC before parsing, then render local time.
 */
export function localStamp(s: string): string {
  const d = new Date(/Z|[+-]\d{2}:\d{2}$/.test(s) ? s : s.replace(" ", "T") + "Z")
  return Number.isNaN(d.getTime())
    ? s.slice(0, 16).replace("T", " ")
    : d.toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short", hour12: false })
}

/** Cycle report browser: list on the left, rendered markdown on the right. */
export function ReportView() {
  const [list, setList] = useState<ReportSummary[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .reports()
      .then((rows) => {
        if (!alive) return
        setList(rows)
        if (rows.length > 0) setSelected(rows[0].id)
      })
      .catch(() => {
        if (alive) setError("리포트 목록을 불러오지 못했습니다 (백엔드 연결 확인)")
      })
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    if (selected === null) return
    let alive = true
    setReport(null)
    api
      .report(selected)
      .then((r) => {
        if (alive) setReport(r)
      })
      .catch(() => {
        if (alive) setError("리포트를 불러오지 못했습니다")
      })
    return () => {
      alive = false
    }
  }, [selected])

  return (
    <div className="report-view">
      {error && <div className="panel-notice">{error}</div>}
      {list !== null && list.length === 0 && (
        <div className="panel-empty">아직 리포트가 없습니다 — 사이클을 완료하면 생성됩니다</div>
      )}
      {list !== null && list.length > 0 && (
        <div className="report-layout">
          <nav className="report-list">
            {list.map((r) => (
              <button
                key={r.id}
                type="button"
                className={`report-item ${selected === r.id ? "report-item-active" : ""}`}
                onClick={() => setSelected(r.id)}
              >
                <span className="report-cycle">
                  <span className={`badge report-kind report-kind-${r.kind ?? "research"}`}>
                    {reportKindLabel(r.kind)}
                  </span>
                  사이클 #{r.cycle_id}
                </span>
                <span className="report-date">{localStamp(r.created_at)}</span>
              </button>
            ))}
          </nav>
          <article className="report-body">
            {report ? renderMarkdown(report.markdown) : <div className="panel-empty">불러오는 중…</div>}
          </article>
        </div>
      )}
    </div>
  )
}
