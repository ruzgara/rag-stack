#!/usr/bin/env python3
"""
Accounts from the command line, for everything the admin UI doesn't exist for
yet -- and for the first admin, who has to be created before anyone can sign
in to create anyone.

Passwords are always prompted for, never taken as an argument: an argument
ends up in shell history and in `ps`.

Run through ragctl:
  ragctl user add alice@example.com --admin      create a user (prompts)
  ragctl user add bob@example.com --password-stdin  ...or scripted, from stdin
  ragctl user list                               users, with groups and grants
  ragctl user passwd alice@example.com           change a password
  ragctl user disable alice@example.com          keep the history, refuse logins
  ragctl user delete alice@example.com
  ragctl user group add Analysts                 groups (locked: admin-only membership)
  ragctl user group add Everyone --type open     open: users can join themselves
  ragctl user group list
  ragctl user member add Analysts alice@example.com
  ragctl user grant Analysts chat.use            grant to a group or a user
  ragctl user grant alice@example.com library.read --library <id> --deny
  ragctl user revoke Analysts chat.use
  ragctl user permissions                        what the names mean
  ragctl user library add "Project finance"      libraries: named document collections
  ragctl user library list
  ragctl user library documents                  every indexed document, to find a sha
  ragctl user library add-doc "Project finance" infra-finance.pdf
  ragctl user library remove-doc "Project finance" infra-finance.pdf
  ragctl user library delete "Project finance"
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from accounts import PERMISSIONS, Accounts, Invalid
from auth import hash_password
from db import ChatDB, NotFound

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def accounts() -> Accounts:
    return Accounts(ChatDB(DATA_DIR / "chat.db"))


def ask_password(args, prompt: str = "Password: ") -> str:
    """Asks twice, at the terminal. Piping into getpass is refused rather than
    accepted quietly: it echoes the password, and what gets stored then depends
    on how the pipe was buffered -- an account whose password isn't what you
    think it is. --password-stdin is the deliberate way to script it."""
    if getattr(args, "password_stdin", False):
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            sys.exit("--password-stdin: nothing on stdin")
        return password
    if not sys.stdin.isatty():
        sys.exit("run this at a terminal, or pass --password-stdin and pipe the password in")
    first = getpass.getpass(prompt)
    if first != getpass.getpass("Again: "):
        sys.exit("the two passwords don't match")
    return first


def subject_of(acc: Accounts, name: str) -> tuple[str, str]:
    """A user's email or a group's name, as (subject_type, id)."""
    if "@" in name:
        user = acc.by_email(name)
        if not user:
            sys.exit(f"no user {name}")
        return "user", user.id
    for group in acc.groups():
        if group["name"].lower() == name.lower():
            return "group", group["id"]
    sys.exit(f"no group {name!r} (a user is named by email)")


def user_of(acc: Accounts, email: str):
    user = acc.by_email(email)
    if not user:
        sys.exit(f"no user {email}")
    return user


# ------------------------------------------------------------------ commands --

def cmd_add(acc: Accounts, args) -> None:
    password = None if args.no_password else ask_password(args)
    try:
        user = acc.create_user(args.email, hash_password(password) if password else None,
                               args.name, args.admin)
    except Invalid as exc:
        sys.exit(str(exc))
    print(f"created {user.email}{' (admin)' if user.is_admin else ''} id={user.id}")
    if not args.admin and not user.is_admin:
        print("note: no permissions yet. `ragctl user grant "
              f"{user.email} chat.use` lets them ask questions.")


def cmd_list(acc: Accounts, args) -> None:
    users = acc.users()
    if not users:
        print("No users yet. Create the first admin with: ragctl user add you@example.com --admin")
        return
    for user in users:
        flags = " ".join(f for f, on in (("admin", user.is_admin), ("disabled", user.disabled)) if on)
        print(f"{user.email}{'  [' + flags + ']' if flags else ''}")
        print(f"    id      {user.id}")
        if user.groups:
            print(f"    groups  {', '.join(user.groups)}")
        if not user.password_hash:
            print("    login   no password (proxy mode, or not set yet)")
        granted = acc.effective_permissions(user.id)
        shown = ", ".join(f"{name}{'' if libs == {None} else ' ' + str(sorted(str(x) for x in libs))}"
                          for name, libs in sorted(granted.items())) or "none"
        print(f"    allowed {shown}")


def cmd_passwd(acc: Accounts, args) -> None:
    user = user_of(acc, args.email)
    try:
        acc.set_password(user.id, hash_password(ask_password(args, f"New password for {user.email}: ")))
    except Invalid as exc:
        sys.exit(str(exc))
    print(f"password changed; {user.email} is signed out everywhere")


def cmd_disable(acc: Accounts, args) -> None:
    user = acc.update_user(user_of(acc, args.email).id, disabled=not args.enable)
    print(f"{user.email} is now {'enabled' if not user.disabled else 'disabled'}")


def cmd_admin(acc: Accounts, args) -> None:
    user = acc.update_user(user_of(acc, args.email).id, is_admin=not args.revoke)
    print(f"{user.email} is {'an admin' if user.is_admin else 'no longer an admin'}")


def cmd_delete(acc: Accounts, args) -> None:
    user = user_of(acc, args.email)
    if input(f"Delete {user.email}? Their conversations are kept. [y/N] ").strip().lower() != "y":
        sys.exit("cancelled")
    acc.delete_user(user.id)
    print(f"deleted {user.email}")


def cmd_group(acc: Accounts, args) -> None:
    if args.action == "add":
        try:
            group = acc.create_group(args.name, kind=args.type)
        except Invalid as exc:
            sys.exit(str(exc))
        print(f"created {group['type']} group {group['name']} id={group['id']}")
    elif args.action == "delete":
        _, group_id = subject_of(acc, args.name)
        acc.delete_group(group_id)
        print(f"deleted group {args.name}")
    else:
        for group in acc.groups():
            grants = acc.grants_of("group", group["id"])
            print(f"{group['name']}  id={group['id']}  type={group['type']}  members={len(group['members'])}")
            for grant in grants:
                where = f" on {grant['library_id']}" if grant["library_id"] else ""
                print(f"    {grant['effect']:5s} {grant['permission']}{where}")


def cmd_member(acc: Accounts, args) -> None:
    _, group_id = subject_of(acc, args.group)
    user = user_of(acc, args.email)
    if args.action == "add":
        acc.add_member(group_id, user.id)
        print(f"{user.email} joined {args.group}")
    else:
        acc.remove_member(group_id, user.id)
        print(f"{user.email} left {args.group}")


def cmd_grant(acc: Accounts, args) -> None:
    subject_type, subject_id = subject_of(acc, args.subject)
    try:
        if args.revoking:
            acc.revoke(subject_type, subject_id, args.permission, args.library)
            print(f"revoked {args.permission} from {args.subject}")
        else:
            effect = "deny" if args.deny else "allow"
            acc.grant(subject_type, subject_id, args.permission, args.library, effect)
            print(f"{effect} {args.permission}{' on ' + args.library if args.library else ''} for {args.subject}")
    except (Invalid, NotFound) as exc:
        sys.exit(str(exc))


def resolve_doc_sha(acc: Accounts, query: str) -> str:
    """A document's sha256, from a full sha or a (sub)match against an
    indexed filename -- so a library's documents can be managed by name
    without typing a hash."""
    import re

    from library import manifest_documents

    query = query.strip()
    if re.fullmatch(r"[0-9a-f]{64}", query.lower()):
        return query.lower()
    wanted = query.lower()
    matches = [(sha, rec.get("filename") or sha) for sha, rec in manifest_documents().items()
              if wanted in (rec.get("filename") or "").lower()]
    if not matches:
        sys.exit(f"no indexed document matches {query!r}. `ragctl user library documents` lists them.")
    if len(matches) > 1:
        sys.exit(f"{query!r} matches more than one document: {', '.join(n for _, n in matches)}. "
                 "Use the sha256 instead.")
    return matches[0][0]


def library_of(acc: Accounts, name: str) -> dict:
    wanted = name.strip().lower()
    for lib in acc.libraries():
        if lib["id"] == name or lib["name"].lower() == wanted:
            return lib
    sys.exit(f"no library {name!r}")


def cmd_library(acc: Accounts, args) -> None:
    if args.action == "add":
        try:
            lib = acc.create_library(args.name, args.description)
        except Invalid as exc:
            sys.exit(str(exc))
        print(f"created library {lib['name']} id={lib['id']}")
    elif args.action == "list":
        libs = acc.libraries()
        if not libs:
            print("No libraries yet. Create one with: ragctl user library add \"Name\"")
            return
        for lib in libs:
            print(f"{lib['name']}  id={lib['id']}  documents={lib['document_count']}")
    elif args.action == "delete":
        lib = library_of(acc, args.name)
        acc.delete_library(lib["id"])
        print(f"deleted library {lib['name']} (grants on it and its attachments to chats are gone too)")
    elif args.action == "documents":
        from library import manifest_documents

        docs = manifest_documents()
        if not docs:
            print("No documents indexed yet.")
            return
        names = {lib["id"]: lib["name"] for lib in acc.libraries()}
        for sha, rec in docs.items():
            print(f"{rec.get('filename') or sha}")
            print(f"    sha256   {sha}")
            in_libs = [names.get(lib_id, lib_id) for lib_id in acc.libraries_of(sha)]
            print(f"    in       {', '.join(in_libs) or '(no library)'}")
    else:  # add-doc, remove-doc
        lib = library_of(acc, args.name)
        sha = resolve_doc_sha(acc, args.document)
        if args.action == "add-doc":
            acc.add_document(lib["id"], sha)
            print(f"added {sha[:12]}… to {lib['name']}")
        else:
            acc.remove_document(lib["id"], sha)
            print(f"removed {sha[:12]}… from {lib['name']}")


def cmd_permissions(acc: Accounts, args) -> None:
    for name, describes in PERMISSIONS.items():
        scope = "per library" if name.startswith("library.") else "global"
        print(f"{name:20s} {scope:11s} {describes}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ragctl user", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    add = subs.add_parser("add", help="create a user (prompts for a password)")
    add.add_argument("email")
    add.add_argument("--admin", action="store_true", help="full permissions, and can manage users")
    add.add_argument("--name", default=None, help="display name")
    add.add_argument("--no-password", action="store_true", help="for QUERY_AUTH_MODE=proxy")
    add.add_argument("--password-stdin", action="store_true", help="read the password from stdin instead of asking")
    add.set_defaults(run=cmd_add)

    subs.add_parser("list", help="list users, their groups and what they may do").set_defaults(run=cmd_list)

    passwd = subs.add_parser("passwd", help="change a password")
    passwd.add_argument("email")
    passwd.add_argument("--password-stdin", action="store_true", help="read the password from stdin instead of asking")
    passwd.set_defaults(run=cmd_passwd)

    disable = subs.add_parser("disable", help="refuse logins without deleting anything")
    disable.add_argument("email")
    disable.add_argument("--enable", action="store_true", help="undo it")
    disable.set_defaults(run=cmd_disable)

    admin = subs.add_parser("admin", help="make someone an admin")
    admin.add_argument("email")
    admin.add_argument("--revoke", action="store_true", help="take it away instead")
    admin.set_defaults(run=cmd_admin)

    delete = subs.add_parser("delete", help="delete a user (their conversations are kept)")
    delete.add_argument("email")
    delete.set_defaults(run=cmd_delete)

    group = subs.add_parser("group", help="add, delete or list groups")
    group.add_argument("action", choices=["add", "delete", "list"])
    group.add_argument("name", nargs="?", default=None)
    group.add_argument("--type", choices=["open", "locked"], default="locked",
                       help="add only: open groups are self-joinable; locked ones need an admin (default)")
    group.set_defaults(run=cmd_group)

    mem = subs.add_parser("member", help="add or remove a group member")
    mem.add_argument("action", choices=["add", "remove"])
    mem.add_argument("group")
    mem.add_argument("email")
    mem.set_defaults(run=cmd_member)

    for name, revoking in (("grant", False), ("revoke", True)):
        cmd = subs.add_parser(name, help=f"{name} a permission for a user (by email) or a group (by name)")
        cmd.add_argument("subject")
        cmd.add_argument("permission", choices=sorted(PERMISSIONS))
        cmd.add_argument("--library", default=None, help="library id, for the library.* permissions")
        if not revoking:
            cmd.add_argument("--deny", action="store_true",
                             help="record a refusal, which overrides the subject's groups")
        cmd.set_defaults(run=cmd_grant, revoking=revoking, deny=False)

    subs.add_parser("permissions", help="list the permission names").set_defaults(run=cmd_permissions)

    lib = subs.add_parser("library", help="add, delete or list libraries, and assign documents to them")
    lib.add_argument("action", choices=["add", "delete", "list", "documents", "add-doc", "remove-doc"])
    lib.add_argument("name", nargs="?", default=None, help="library name or id")
    lib.add_argument("document", nargs="?", default=None, help="a document's sha256, or a filename to match")
    lib.add_argument("--description", default=None)
    lib.set_defaults(run=cmd_library)

    args = parser.parse_args()
    if args.command == "group" and args.action in ("add", "delete") and not args.name:
        parser.error(f"group {args.action} needs a name")
    if args.command == "library":
        if args.action in ("add", "delete", "add-doc", "remove-doc") and not args.name:
            parser.error(f"library {args.action} needs a name")
        if args.action in ("add-doc", "remove-doc") and not args.document:
            parser.error(f"library {args.action} needs a document (sha256 or filename)")
    args.run(accounts(), args)


if __name__ == "__main__":
    main()
