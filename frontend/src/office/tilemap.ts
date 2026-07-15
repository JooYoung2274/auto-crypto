// Static office background, rendered ONCE to an offscreen canvas at native
// resolution (320x224); the engine blits it scaled 3x each frame.
// Everything is drawn programmatically — no external assets.

import { COLS, DESKS, DOOR_X, MEETING_TABLE, ROWS, SEATS, TILE, WORLD_H, WORLD_W } from "./waypoints"

const C = {
  wallTop: "#191724",
  wallFace: "#242134",
  wallEdge: "#100e18",
  floorA: "#2e2b3f",
  floorB: "#343149",
  roomFloorA: "#2b3347",
  roomFloorB: "#303a52",
  deskTop: "#8a5a38",
  deskEdge: "#5f3d24",
  monitorBezel: "#191922",
  monitorScreen: "#233a4c",
  monitorStand: "#30303c",
  keyboard: "#3c3c4c",
  chair: "#41415a",
  chairEdge: "#2b2b3c",
  tableTop: "#7a4a2e",
  tableEdge: "#54321e",
  paper: "#e8e4d8",
  plantPot: "#a15b2e",
  plantPotDark: "#79441f",
  plantLeaf: "#3f9e5f",
  plantLeafDark: "#2c7343",
  rug: "#3b3355",
  cooler: "#b9c7d6",
  coolerWater: "#5aa9e6",
}

/** Tiles occupied by walls (row 0, side columns, meeting-room partition). */
export function isWall(x: number, y: number): boolean {
  if (y === 0 || x === 0 || x === COLS - 1 || y === ROWS - 1) return true
  if (x === 14 && y >= 1 && y <= 8) return true // room left wall
  if (y === 8 && x >= 14 && x <= COLS - 1 && x !== DOOR_X) return true // room bottom wall, door gap at (15,8)
  return false
}

function isMeetingRoomFloor(x: number, y: number): boolean {
  return x >= 15 && x <= COLS - 2 && y >= 1 && y <= 7
}

function drawWallTile(ctx: CanvasRenderingContext2D, x: number, y: number): void {
  const px = x * TILE
  const py = y * TILE
  ctx.fillStyle = C.wallFace
  ctx.fillRect(px, py, TILE, TILE)
  ctx.fillStyle = C.wallTop
  ctx.fillRect(px, py, TILE, 6)
  ctx.fillStyle = C.wallEdge
  ctx.fillRect(px, py + TILE - 2, TILE, 2)
}

function drawFloor(ctx: CanvasRenderingContext2D): void {
  for (let y = 0; y < ROWS; y += 1) {
    for (let x = 0; x < COLS; x += 1) {
      if (isWall(x, y)) {
        drawWallTile(ctx, x, y)
        continue
      }
      const room = isMeetingRoomFloor(x, y)
      const even = (x + y) % 2 === 0
      ctx.fillStyle = room ? (even ? C.roomFloorA : C.roomFloorB) : even ? C.floorA : C.floorB
      ctx.fillRect(x * TILE, y * TILE, TILE, TILE)
    }
  }
}

/** Screen rect (px) of the monitor on desk `i` — the engine adds a glow here while the agent works. */
export function monitorScreenRect(deskIndex: number): { x: number; y: number; w: number; h: number } {
  const d = DESKS[deskIndex]
  return { x: d.x * TILE + 9, y: d.y * TILE - 6, w: 14, h: 9 }
}

function drawDesk(ctx: CanvasRenderingContext2D, i: number): void {
  const d = DESKS[i]
  const px = d.x * TILE
  const py = d.y * TILE
  const w = TILE * 2

  // chair (under the seat tile, drawn before the character passes over it)
  const seat = SEATS[i]
  ctx.fillStyle = C.chairEdge
  ctx.fillRect(seat.x * TILE + 3, seat.y * TILE + 4, 10, 10)
  ctx.fillStyle = C.chair
  ctx.fillRect(seat.x * TILE + 4, seat.y * TILE + 5, 8, 8)

  // desk surface
  ctx.fillStyle = C.deskEdge
  ctx.fillRect(px, py + 2, w, TILE - 2)
  ctx.fillStyle = C.deskTop
  ctx.fillRect(px, py, w, TILE - 4)

  // monitor (bezel + screen + stand) sitting on the desk
  const scr = monitorScreenRect(i)
  ctx.fillStyle = C.monitorStand
  ctx.fillRect(scr.x + scr.w / 2 - 2, scr.y + scr.h, 4, 3)
  ctx.fillStyle = C.monitorBezel
  ctx.fillRect(scr.x - 1, scr.y - 1, scr.w + 2, scr.h + 2)
  ctx.fillStyle = C.monitorScreen
  ctx.fillRect(scr.x, scr.y, scr.w, scr.h)
  // faint idle scanline
  ctx.fillStyle = "rgba(140, 210, 255, 0.25)"
  ctx.fillRect(scr.x + 2, scr.y + 2, scr.w - 6, 1)

  // keyboard + mug
  ctx.fillStyle = C.keyboard
  ctx.fillRect(px + 8, py + 8, 12, 4)
  ctx.fillStyle = "#c96f6f"
  ctx.fillRect(px + w - 8, py + 6, 4, 5)
}

function drawMeetingTable(ctx: CanvasRenderingContext2D): void {
  const px = MEETING_TABLE.x * TILE
  const py = MEETING_TABLE.y * TILE
  const w = MEETING_TABLE.w * TILE
  const h = MEETING_TABLE.h * TILE

  // rug under the table for coziness
  ctx.fillStyle = C.rug
  ctx.fillRect(px - 8, py - 6, w + 16, h + 12)

  ctx.fillStyle = C.tableEdge
  ctx.fillRect(px + 1, py + 3, w - 2, h - 2)
  ctx.fillStyle = C.tableTop
  ctx.fillRect(px + 1, py + 1, w - 2, h - 4)

  // scattered papers + a laptop
  ctx.fillStyle = C.paper
  ctx.fillRect(px + 5, py + 7, 7, 9)
  ctx.fillRect(px + 19, py + 24, 7, 9)
  ctx.fillStyle = C.monitorBezel
  ctx.fillRect(px + 17, py + 8, 10, 7)
  ctx.fillStyle = C.monitorScreen
  ctx.fillRect(px + 18, py + 9, 8, 5)
}

function drawPlant(ctx: CanvasRenderingContext2D, tx: number, ty: number): void {
  const px = tx * TILE
  const py = ty * TILE
  ctx.fillStyle = C.plantPotDark
  ctx.fillRect(px + 4, py + 10, 8, 5)
  ctx.fillStyle = C.plantPot
  ctx.fillRect(px + 4, py + 9, 8, 3)
  ctx.fillStyle = C.plantLeafDark
  ctx.fillRect(px + 3, py + 4, 4, 5)
  ctx.fillRect(px + 9, py + 3, 4, 6)
  ctx.fillStyle = C.plantLeaf
  ctx.fillRect(px + 5, py + 1, 5, 7)
  ctx.fillRect(px + 2, py + 5, 3, 3)
  ctx.fillRect(px + 11, py + 5, 3, 3)
}

function drawWaterCooler(ctx: CanvasRenderingContext2D, tx: number, ty: number): void {
  const px = tx * TILE
  const py = ty * TILE
  ctx.fillStyle = C.cooler
  ctx.fillRect(px + 5, py + 6, 7, 9)
  ctx.fillStyle = C.coolerWater
  ctx.fillRect(px + 6, py + 1, 5, 6)
  ctx.fillStyle = "rgba(255,255,255,0.6)"
  ctx.fillRect(px + 7, py + 2, 1, 4)
}

/** Render the whole static background to a fresh offscreen canvas (native res). */
export function renderBackground(): HTMLCanvasElement {
  const canvas = document.createElement("canvas")
  canvas.width = WORLD_W
  canvas.height = WORLD_H
  const ctx = canvas.getContext("2d")
  if (!ctx) throw new Error("2d context unavailable")
  ctx.imageSmoothingEnabled = false

  drawFloor(ctx)
  drawMeetingTable(ctx)
  for (let i = 0; i < DESKS.length; i += 1) drawDesk(ctx, i)
  drawPlant(ctx, 12, 1)
  drawPlant(ctx, 18, 1)
  drawPlant(ctx, 1, 12)
  drawPlant(ctx, 11, 12)
  drawWaterCooler(ctx, 17, 11)

  // door mat at the meeting-room door gap
  ctx.fillStyle = "#4a415f"
  ctx.fillRect(DOOR_X * TILE + 2, 8 * TILE + 4, TILE - 4, TILE - 8)

  return canvas
}
