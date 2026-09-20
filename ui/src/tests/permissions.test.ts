import { describe, expect, it } from "vitest";
import { can, readableLibraries } from "../lib/permissions";
import type { Me, PublicUser } from "../lib/types";

function user(overrides: Partial<PublicUser> = {}): PublicUser {
  return {
    id: "u1",
    email: "a@b.com",
    display_name: null,
    is_admin: false,
    disabled: false,
    groups: [],
    created_at: 0,
    last_login_at: null,
    ...overrides,
  };
}

function me(permissions: Record<string, string[]>, userOverrides: Partial<PublicUser> = {}): Me {
  return { user: user(userOverrides), permissions, auth_mode: "password" };
}

describe("can", () => {
  it("is false when signed out", () => {
    expect(can(null, "chat.use")).toBe(false);
  });

  it("is true for an admin regardless of the permissions map", () => {
    expect(can(me({}, { is_admin: true }), "chat.use")).toBe(true);
  });

  it("is true for the literal ['None'] global-permission marker", () => {
    expect(can(me({ "chat.use": ["None"] }), "chat.use")).toBe(true);
  });

  it("is true for the wildcard ['*']", () => {
    expect(can(me({ "chat.use": ["*"] }), "chat.use")).toBe(true);
  });

  it("is false when the key is absent", () => {
    expect(can(me({}), "chat.use")).toBe(false);
  });

  it("is false for an empty array", () => {
    expect(can(me({ "chat.use": [] }), "chat.use")).toBe(false);
  });

  it("is false for a disabled user even with a grant", () => {
    expect(can(me({ "chat.use": ["*"] }, { disabled: true }), "chat.use")).toBe(false);
  });
});

describe("readableLibraries", () => {
  it("is 'all' for an admin", () => {
    expect(readableLibraries(me({}, { is_admin: true }))).toBe("all");
  });

  it("is 'all' for a wildcard grant", () => {
    expect(readableLibraries(me({ "library.read": ["*"] }))).toBe("all");
  });

  it("is the specific set of library ids for a scoped grant", () => {
    const result = readableLibraries(me({ "library.read": ["lib1", "lib2"] }));
    expect(result).toEqual(new Set(["lib1", "lib2"]));
  });

  it("excludes the 'None' placeholder from a library-scoped set", () => {
    // 'None' only ever appears on a GLOBAL permission in practice, but the
    // filter must not accidentally treat it as a real library id if it did.
    const result = readableLibraries(me({ "library.read": ["None"] }));
    expect(result).toEqual(new Set());
  });

  it("is empty for a signed-out caller", () => {
    expect(readableLibraries(null)).toEqual(new Set());
  });
});
