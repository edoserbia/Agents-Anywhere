import assert from "node:assert/strict"
import test from "node:test"

import { groupProjectsByDevice } from "../src/components/sidebar/project-device-groups.ts"
import { sortDevicesByCreation } from "../src/components/sidebar/device-list-order.ts"

function project(id, connectorId, name = id, workspacePath = `/w/${id}`) {
  return { id, connectorId, name, workspacePath }
}

function device(id, name, status = "online", createdAt = "2026-01-01T00:00:00Z") {
  return { id, name, status, createdAt }
}

test("projects are grouped by device in device-list order", () => {
  const groups = groupProjectsByDevice(
    [
      project("p2", "dev-b", "alpha"),
      project("p1", "dev-a", "alpha"),
      project("p3", "dev-a", "beta"),
    ],
    [device("dev-a", "MacBook"), device("dev-b", "ThinkPad")],
  )

  assert.deepEqual(groups.map((group) => group.connectorId), ["dev-a", "dev-b"])
  assert.deepEqual(groups[0].projects.map((item) => item.id), ["p1", "p3"])
  assert.deepEqual(groups[1].projects.map((item) => item.id), ["p2"])
})

test("same-named projects on different devices stay in separate groups", () => {
  const groups = groupProjectsByDevice(
    [project("p1", "dev-a", "website"), project("p2", "dev-b", "website")],
    [device("dev-a", "MacBook"), device("dev-b", "ThinkPad")],
  )

  assert.equal(groups.length, 2)
  // The project name alone cannot distinguish them; the device label does.
  assert.equal(groups[0].projects[0].name, groups[1].projects[0].name)
  assert.notEqual(groups[0].deviceName, groups[1].deviceName)
})

test("a device with no projects is omitted rather than rendered empty", () => {
  const groups = groupProjectsByDevice(
    [project("p1", "dev-a")],
    [device("dev-a", "MacBook"), device("dev-b", "Unused")],
  )

  assert.deepEqual(groups.map((group) => group.connectorId), ["dev-a"])
})

test("projects on an unknown device are kept in a trailing group", () => {
  const groups = groupProjectsByDevice(
    [project("p1", "dev-a"), project("p9", "dev-gone")],
    [device("dev-a", "MacBook")],
  )

  assert.deepEqual(groups.map((group) => group.connectorId), ["dev-a", "dev-gone"])
  // The raw id is the only label available, and it must still be shown.
  assert.equal(groups[1].deviceName, "dev-gone")
  assert.equal(groups[1].online, false)
})

test("online state follows the device record", () => {
  const groups = groupProjectsByDevice(
    [project("p1", "dev-a"), project("p2", "dev-b")],
    [device("dev-a", "MacBook", "online"), device("dev-b", "ThinkPad", "offline")],
  )

  assert.equal(groups[0].online, true)
  assert.equal(groups[1].online, false)
})

test("no devices means no groups so the caller can fall back to a flat list", () => {
  assert.deepEqual(groupProjectsByDevice([project("p1", "dev-a")], []), [])
})

// ── Device order must follow pairing time, never activity ──────
//
// The server lists connectors by `updated_at DESC`, so a device that reports
// presence or runs a session arrives first on the next poll. Group order has to
// ignore that, otherwise devices swap places while the user is looking at them.

test("device order follows pairing time, not the order the server returned", () => {
  const macbook = device("dev-a", "MacBook", "online", "2026-01-01T00:00:00Z")
  const thinkpad = device("dev-b", "ThinkPad", "online", "2026-03-01T00:00:00Z")

  const projects = [project("p1", "dev-a"), project("p2", "dev-b")]
  const whenMacBookFirst = groupProjectsByDevice(projects, [macbook, thinkpad])
  const whenThinkPadFirst = groupProjectsByDevice(projects, [thinkpad, macbook])

  // Oldest pairing stays on top no matter which order the API used.
  assert.deepEqual(whenMacBookFirst.map((group) => group.connectorId), ["dev-a", "dev-b"])
  assert.deepEqual(whenThinkPadFirst.map((group) => group.connectorId), ["dev-a", "dev-b"])
})

test("a device becoming active does not move it up the list", () => {
  const macbook = device("dev-a", "MacBook", "offline", "2026-01-01T00:00:00Z")
  const thinkpad = device("dev-b", "ThinkPad", "online", "2026-03-01T00:00:00Z")
  const projects = [project("p1", "dev-a"), project("p2", "dev-b")]

  const before = groupProjectsByDevice(projects, [macbook, thinkpad])
  // MacBook comes online and would sort first under the server's ordering.
  const after = groupProjectsByDevice(projects, [
    { ...macbook, status: "online" },
    thinkpad,
  ])

  assert.deepEqual(before.map((group) => group.connectorId), ["dev-a", "dev-b"])
  assert.deepEqual(after.map((group) => group.connectorId), ["dev-a", "dev-b"])
  // The status still updates; only the position is pinned.
  assert.equal(after[0].online, true)
})

test("sortDevicesByCreation ignores activity and presence changes", () => {
  const devices = [
    { id: "c", name: "Third", status: "online", createdAt: "2026-03-01T00:00:00Z" },
    { id: "a", name: "First", status: "offline", createdAt: "2026-01-01T00:00:00Z" },
    { id: "b", name: "Second", status: "online", createdAt: "2026-02-01T00:00:00Z" },
  ]
  const expected = ["a", "b", "c"]

  assert.deepEqual(sortDevicesByCreation(devices).map((item) => item.id), expected)
  // Reordering the input, or flipping every status, must not change anything.
  assert.deepEqual(sortDevicesByCreation([...devices].reverse()).map((item) => item.id), expected)
  assert.deepEqual(
    sortDevicesByCreation(devices.map((item) => ({ ...item, status: "offline" }))).map((item) => item.id),
    expected,
  )
})

test("devices without a pairing time sort last but stay deterministic", () => {
  const devices = [
    { id: "z", name: "Unknown", createdAt: null },
    { id: "a", name: "Known", createdAt: "2026-01-01T00:00:00Z" },
    { id: "y", name: "AlsoUnknown" },
  ]

  assert.deepEqual(sortDevicesByCreation(devices).map((item) => item.id), ["a", "y", "z"])
  assert.deepEqual(sortDevicesByCreation([...devices].reverse()).map((item) => item.id), ["a", "y", "z"])
})

test("an unparsable pairing time is treated as missing rather than crashing", () => {
  const devices = [
    { id: "bad", name: "Bad", createdAt: "not-a-date" },
    { id: "good", name: "Good", createdAt: "2026-01-01T00:00:00Z" },
  ]

  assert.deepEqual(sortDevicesByCreation(devices).map((item) => item.id), ["good", "bad"])
})
