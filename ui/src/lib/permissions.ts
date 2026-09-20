// The permissions trap (api.py:291, accounts.py:317-354): `me().permissions`
// values are ["*"] for an admin, the LITERAL STRING "None" for a non-admin
// holding a global permission (chat.use, users.manage, libraries.manage), or
// an array of library ids for library.read / library.write. A disabled user
// gets {}.

import type { Me } from "./types";

export type GlobalPermission = "chat.use" | "users.manage" | "libraries.manage";
export type LibraryPermission = "library.read" | "library.write";

export function can(me: Me | null, perm: GlobalPermission): boolean {
  if (!me || me.user.disabled) return false;
  if (me.user.is_admin) return true;
  const v = me.permissions[perm];
  return Array.isArray(v) && v.length > 0; // ["*"] or the literal ["None"] both mean yes
}

/** "all" for an admin or a ["*"] grant, otherwise the specific set of
 *  library ids the caller may read/write (excluding the "None" placeholder,
 *  which only ever appears on a GLOBAL permission, never a library one). */
export function readableLibraries(me: Me | null, perm: LibraryPermission = "library.read"): "all" | Set<string> {
  if (!me || me.user.disabled) return new Set();
  if (me.user.is_admin) return "all";
  const v = me.permissions[perm];
  if (!v) return new Set();
  if (v.includes("*")) return "all";
  return new Set(v.filter((x) => x !== "None"));
}

export function canReadLibrary(me: Me | null, libraryId: string): boolean {
  const readable = readableLibraries(me, "library.read");
  return readable === "all" || readable.has(libraryId);
}

export function canWriteLibrary(me: Me | null, libraryId: string): boolean {
  const writable = readableLibraries(me, "library.write");
  return writable === "all" || writable.has(libraryId);
}
