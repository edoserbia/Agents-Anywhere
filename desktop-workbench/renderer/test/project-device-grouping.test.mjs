import assert from "node:assert/strict"
import test from "node:test"

import { groupProjectsByDevice } from "../src/components/sidebar/project-device-groups.ts"

function project(id, connectorId, name = id, workspacePath = `/w/${id}`) {
  return { id, connectorId, name, workspacePath }
}

function device(id, name, status = "online") {
  return { id, name, status }
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
