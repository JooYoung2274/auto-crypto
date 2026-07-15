// Office floor layout + Manhattan waypoint routing. Pure data & functions —
// no canvas/DOM dependency so everything here is unit-testable in node.
//
// Coordinate systems:
//   tiles: 20 x 14 grid, 16 px per tile (native/unscaled), rendered at 3x.
//   px:    unscaled pixels. A character's anchor is the center of the tile it
//          stands on: px = tile * 16 + 8.
//
// Walk network (all axis-aligned):
//   - top desk seats  (y=3) drop to lane y=4
//   - bottom desk seats (y=7) drop to lane y=8
//   - lanes run horizontally to the vertical connector x=13
//   - x=13 runs down to the main corridor y=10
//   - main corridor y=10 runs right to the meeting-room door column x=15
//   - x=15 runs up through the door gap (15,8) into the room (rows 1..7)
//   - meeting seats sit above/below the table at (16,2) and (16,6)

export interface Point {
  x: number
  y: number
}

export const TILE = 16
export const SCALE = 3
export const COLS = 20
export const ROWS = 14
export const WORLD_W = COLS * TILE // 320
export const WORLD_H = ROWS * TILE // 224

/** Vertical connector column between the desk area and the corridor. */
export const VERT_X = 13
/** Main horizontal corridor row. */
export const MAIN_Y = 10
/** Meeting-room door column (gap in the room's bottom wall at (15,8)). */
export const DOOR_X = 15

/** Top-left tile of each 2-tile-wide desk; index i belongs to agent i. */
export const DESKS: Point[] = [
  { x: 1, y: 2 },
  { x: 4, y: 2 },
  { x: 7, y: 2 },
  { x: 10, y: 2 },
  { x: 1, y: 6 },
  { x: 4, y: 6 },
  { x: 7, y: 6 },
]

/** Seat tile for each desk (character stands here, facing the monitor above). */
export const SEATS: Point[] = DESKS.map((d) => ({ x: d.x, y: d.y + 1 }))

/** Meeting-room table occupies tiles cols 16..17, rows 3..5. */
export const MEETING_TABLE = { x: 16, y: 3, w: 2, h: 3 }

/** Two meeting seats: [0] above the table (faces down), [1] below (faces up). */
export const MEETING_SEATS: [Point, Point] = [
  { x: 16, y: 2 },
  { x: 16, y: 6 },
]

/** Center-of-tile pixel coordinate. */
export function toPx(tile: Point): Point {
  return { x: tile.x * TILE + TILE / 2, y: tile.y * TILE + TILE / 2 }
}

const EPS = 0.51

function near(a: number, b: number): boolean {
  return Math.abs(a - b) < EPS
}

function samePoint(a: Point, b: Point): boolean {
  return near(a.x, b.x) && near(a.y, b.y)
}

/** Drop consecutive duplicate points (zero-length segments). */
function dedupe(points: Point[]): Point[] {
  const out: Point[] = []
  for (const p of points) {
    const last = out[out.length - 1]
    if (!last || !samePoint(last, p)) out.push(p)
  }
  return out
}

/**
 * Waypoints (px) leading from an arbitrary on-network position to the main
 * corridor row. Positions produced by walking our own routes are always on
 * the network, so an exact classification is possible.
 */
function projectToCorridor(pos: Point): Point[] {
  const corridorY = toPx({ x: 0, y: MAIN_Y }).y
  const vertX = toPx({ x: VERT_X, y: 0 }).x
  const doorX = toPx({ x: DOOR_X, y: 0 }).x

  if (near(pos.y, corridorY)) return [] // already on the corridor
  if (near(pos.x, vertX)) return [{ x: vertX, y: corridorY }]
  if (near(pos.x, doorX)) return [{ x: doorX, y: corridorY }]
  // Inside the meeting room (right of the door column): exit via the door column.
  if (pos.x > doorX) {
    return [
      { x: doorX, y: pos.y },
      { x: doorX, y: corridorY },
    ]
  }
  const laneTopY = toPx({ x: 0, y: 4 }).y
  const laneBottomY = toPx({ x: 0, y: 8 }).y
  // On a horizontal lane: head to the vertical connector, then down.
  if (near(pos.y, laneTopY) || near(pos.y, laneBottomY)) {
    return [
      { x: vertX, y: pos.y },
      { x: vertX, y: corridorY },
    ]
  }
  // On a seat column between the seat and its lane: drop to the nearer lane.
  const laneY = pos.y <= laneTopY ? laneTopY : laneBottomY
  return [
    { x: pos.x, y: laneY },
    { x: vertX, y: laneY },
    { x: vertX, y: corridorY },
  ]
}

/** Route (px waypoints, first = current position) to a desk seat tile. */
export function routeToSeat(posPx: Point, seat: Point): Point[] {
  const target = toPx(seat)
  if (samePoint(posPx, target)) return [posPx]
  const corridorY = toPx({ x: 0, y: MAIN_Y }).y
  const vertX = toPx({ x: VERT_X, y: 0 }).x
  const laneY = toPx({ x: 0, y: seat.y + 1 }).y
  return dedupe([
    posPx,
    ...projectToCorridor(posPx),
    { x: vertX, y: corridorY },
    { x: vertX, y: laneY },
    { x: target.x, y: laneY },
    target,
  ])
}

/** Route (px waypoints, first = current position) to a meeting seat tile. */
export function routeToMeetingSeat(posPx: Point, meetingSeat: Point): Point[] {
  const target = toPx(meetingSeat)
  if (samePoint(posPx, target)) return [posPx]
  const corridorY = toPx({ x: 0, y: MAIN_Y }).y
  const doorX = toPx({ x: DOOR_X, y: 0 }).x
  return dedupe([
    posPx,
    ...projectToCorridor(posPx),
    { x: doorX, y: corridorY },
    { x: doorX, y: target.y },
    target,
  ])
}

export interface StepResult {
  pos: Point
  idx: number
  arrived: boolean
}

/**
 * Advance `dist` px along `path` (px waypoints). `idx` is the index of the
 * next waypoint to reach (start with 1; path[0] is the start position).
 * Overshoot is carried into the next segment and the final waypoint is hit
 * exactly — no drifting past the target.
 */
export function stepAlongPath(pos: Point, path: Point[], idx: number, dist: number): StepResult {
  let x = pos.x
  let y = pos.y
  let i = idx
  let remaining = dist
  while (remaining > 0 && i < path.length) {
    const target = path[i]
    const dx = target.x - x
    const dy = target.y - y
    const segment = Math.abs(dx) + Math.abs(dy) // axis-aligned by construction
    if (segment <= remaining) {
      x = target.x
      y = target.y
      remaining -= segment
      i += 1
    } else {
      // Move along one axis at a time (x first) — segments are axis-aligned,
      // so in practice only one of dx/dy is non-zero.
      const moveX = Math.sign(dx) * Math.min(Math.abs(dx), remaining)
      x += moveX
      remaining -= Math.abs(moveX)
      if (remaining > 0) {
        const moveY = Math.sign(dy) * Math.min(Math.abs(dy), remaining)
        y += moveY
        remaining -= Math.abs(moveY)
      }
    }
  }
  return { pos: { x, y }, idx: i, arrived: i >= path.length }
}
