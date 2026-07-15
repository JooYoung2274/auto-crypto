import { useEffect, useRef } from "react"
import { OfficeEngine } from "../office/engine"
import type { AgentMeta } from "../office/engine"

interface Props {
  agents: AgentMeta[]
  /** Called with the live engine after mount and with null before unmount. */
  onEngine?: (engine: OfficeEngine | null) => void
}

/**
 * Thin React wrapper around the framework-free OfficeEngine. StrictMode-safe:
 * every effect run builds a fresh engine and the cleanup destroys it.
 */
export function OfficeCanvas({ agents, onEngine }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const onEngineRef = useRef(onEngine)
  onEngineRef.current = onEngine

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const engine = new OfficeEngine(canvas, agents)
    engine.start()
    onEngineRef.current?.(engine)
    return () => {
      onEngineRef.current?.(null)
      engine.destroy()
    }
  }, [agents])

  return <canvas ref={canvasRef} className="office-canvas" />
}
