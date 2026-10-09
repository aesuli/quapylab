import { FormEvent, useEffect, useMemo, useState } from "react";
import { WorkflowEditor } from "./WorkflowEditor";

type Role = "owner" | "administrator" | "editor" | "viewer";
type Session = {
  authenticated: boolean;
  deployment_mode: "desktop" | "web";
  user_id: string;
  active_group_id: string;
  csrf_token: string | null;
  is_system_administrator?: boolean;
};
type ManagedUser = {
  id: string;
  login_name: string;
  display_name: string;
  is_system_administrator: boolean;
  is_active: boolean;
};
type Project = {
  id: string;
  name: string;
  description: string;
  settings: { quapy_environment?: { SAMPLE_SIZE?: number | null } };
};
type Group = {
  id: string;
  name: string;
  slug: string;
  type: "personal" | "shared";
  role?: Role;
};
type Member = {
  user_id: string;
  role: Role;
  login_name: string | null;
  display_name: string | null;
};
type ApiError = { error?: { message?: string } };
type NativeExportApi = {
  download_export: (kind: string, resourceId: string, includeDatasets: boolean) => Promise<{ saved: boolean; cancelled?: boolean; error?: string }>;
};
const roles: Role[] = ["owner", "administrator", "editor", "viewer"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    throw new Error(body.error?.message ?? "The request could not be completed.");
  }
  return response.status === 204 ? (undefined as T) : (response.json() as Promise<T>);
}
const csrf = (session: Session): Record<string, string> => (session.csrf_token ? { "X-CSRF-Token": session.csrf_token } : {});
const errorText = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback);
const slugify = (value: string) =>
  value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
function Notice({ children, dismiss }: { children: string; dismiss: () => void }) {
  return (
    <div className="form-error feedback-message" role="alert">
      <span>{children}</span>
      <button className="icon-button" type="button" aria-label="Dismiss notification" onClick={dismiss}>
        ×
      </button>
    </div>
  );
}

function Login({ onAuthenticated }: { onAuthenticated: (session: Session) => void }) {
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await request("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login_name: loginName, password }),
      });
      onAuthenticated(await request<Session>("/api/v1/auth/session"));
    } catch (reason) {
      setError(errorText(reason, "Login failed."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-shell">
      <form className="login-card" onSubmit={submit}>
        <p className="eyebrow">QuaPy Lab</p>
        <h1>Welcome back</h1>
        <p>Sign in to your reproducible quantification workspace.</p>
        <label>
          Login name
          <input autoComplete="username" value={loginName} onChange={(e) => setLoginName(e.target.value)} />
        </label>
        <label>
          Password
          <input autoComplete="current-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <Notice dismiss={() => setError(null)}>{error}</Notice>}
        <button disabled={busy} type="submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

function AccountPanel({ session, close }: { session: Session; close: () => void }) {
  const admin = Boolean(session.is_system_administrator);
  const json = { "Content-Type": "application/json", ...csrf(session) };
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loginName, setLoginName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const reloadUsers = async () => setUsers((await request<{ items: ManagedUser[] }>("/api/v1/users")).items);
  useEffect(() => {
    if (admin) void reloadUsers().catch((reason) => setError(errorText(reason, "Could not load users.")));
  }, [admin]);
  async function run(action: () => Promise<void>, fallback: string, done?: string) {
    setError(null);
    setMessage(null);
    try {
      await action();
      if (done) setMessage(done);
    } catch (reason) {
      setError(errorText(reason, fallback));
    }
  }
  async function changePassword(event: FormEvent) {
    event.preventDefault();
    if (next !== confirm) {
      setError("The new passwords do not match.");
      return;
    }
    await run(
      async () => {
        await request("/api/v1/auth/password", { method: "POST", headers: json, body: JSON.stringify({ current_password: current, new_password: next }) });
        setCurrent("");
        setNext("");
        setConfirm("");
      },
      "Could not change the password.",
      "Password changed.",
    );
  }
  async function createUser(event: FormEvent) {
    event.preventDefault();
    await run(
      async () => {
        await request("/api/v1/users", {
          method: "POST",
          headers: json,
          body: JSON.stringify({ login_name: loginName, display_name: displayName || loginName, password, is_system_administrator: isAdmin }),
        });
        setLoginName("");
        setDisplayName("");
        setPassword("");
        setIsAdmin(false);
        await reloadUsers();
      },
      "Could not create the user.",
      "User created.",
    );
  }
  async function resetPassword(user: ManagedUser) {
    const value = window.prompt(`New password for ${user.login_name} (at least 12 characters). Their sessions will be signed out.`);
    if (!value) return;
    await run(
      () => request<void>(`/api/v1/users/${user.id}/password`, { method: "POST", headers: json, body: JSON.stringify({ new_password: value }) }),
      "Could not reset the password.",
      `Password reset for ${user.login_name}.`,
    );
  }
  async function deleteUser(user: ManagedUser) {
    if (!window.confirm(`Delete user ${user.login_name}? Their personal workspace becomes inaccessible.`)) return;
    await run(
      async () => {
        await request<void>(`/api/v1/users/${user.id}`, { method: "DELETE", headers: csrf(session) });
        await reloadUsers();
      },
      "Could not delete the user.",
      `User ${user.login_name} deleted.`,
    );
  }
  return (
    <aside className="members-panel account-panel" aria-label="Account">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Account</p>
          <h2>Settings</h2>
        </div>
        <button className="panel-close" aria-label="Close account panel" onClick={close}>
          ×
        </button>
      </div>
      {error && <Notice dismiss={() => setError(null)}>{error}</Notice>}
      {message && <p className="panel-copy" role="status">{message}</p>}
      <form className="member-form" onSubmit={changePassword}>
        <h3>Change my password</h3>
        <label>
          Current password
          <input required type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label>
          New password (min. 12 characters)
          <input required minLength={12} type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </label>
        <label>
          Confirm new password
          <input required type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </label>
        <button type="submit">Change password</button>
      </form>
      {admin && (
        <>
          <div className="member-list">
            <h3>Users</h3>
            {users.map((user) => (
              <div className="user-row" key={user.id}>
                <div>
                  <strong>{user.display_name || user.login_name}</strong>
                  <span>
                    @{user.login_name}
                    {user.is_system_administrator ? " · admin" : ""}
                  </span>
                </div>
                <button className="quiet-button dark" onClick={() => void resetPassword(user)}>
                  Reset password
                </button>
                {user.id !== session.user_id && (
                  <button className="icon-button danger" aria-label={`Delete ${user.login_name}`} onClick={() => void deleteUser(user)}>
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>
          <form className="member-form" onSubmit={createUser}>
            <h3>Create user</h3>
            <label>
              Login name
              <input required autoComplete="off" value={loginName} onChange={(e) => setLoginName(e.target.value)} />
            </label>
            <label>
              Display name
              <input autoComplete="off" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            </label>
            <label>
              Initial password (min. 12 characters)
              <input required minLength={12} type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </label>
            <label className="checkbox-label">
              <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} /> Administrator
            </label>
            <button type="submit">Create user</button>
          </form>
        </>
      )}
    </aside>
  );
}

function Members({ session, group, members, close, reload }: { session: Session; group: Group; members: Member[]; close: () => void; reload: () => Promise<void> }) {
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<Role>("editor");
  const [error, setError] = useState<string | null>(null);
  const owner = group.role === "owner";
  const manages = owner || group.role === "administrator";
  const choices = roles;
  const [directory, setDirectory] = useState<{ id: string; login_name: string; display_name: string }[]>([]);
  useEffect(() => {
    if (!manages) return;
    request<{ items: typeof directory }>("/api/v1/users/directory")
      .then((result) => setDirectory(result.items))
      .catch((reason) => setError(errorText(reason, "Could not load users.")));
  }, [manages]);
  const candidates = directory.filter((user) => !members.some((member) => member.user_id === user.id));
  async function add(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await request(`/api/v1/groups/${group.id}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ user_id: userId.trim(), role }),
      });
      setUserId("");
      await reload();
    } catch (reason) {
      setError(errorText(reason, "Could not add member."));
    }
  }
  async function change(member: Member, next: Role) {
    setError(null);
    try {
      await request(`/api/v1/groups/${group.id}/members/${member.user_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ role: next }),
      });
      await reload();
    } catch (reason) {
      setError(errorText(reason, "Could not update the role."));
      await reload();
    }
  }
  async function remove(member: Member) {
    if (!window.confirm(`Remove ${member.display_name || member.login_name || member.user_id} from this group?`)) return;
    setError(null);
    try {
      await request<void>(`/api/v1/groups/${group.id}/members/${member.user_id}`, { method: "DELETE", headers: csrf(session) });
      await reload();
    } catch (reason) {
      setError(errorText(reason, "Could not remove member."));
    }
  }
  return (
    <aside className="members-panel" aria-label="Group members">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Shared group</p>
          <h2>{group.name}</h2>
        </div>
        <button className="quiet-button" onClick={close}>
          Close
        </button>
      </div>
      <p className="panel-copy">
        Your role: <strong>{group.role}</strong>
      </p>
      {error && <Notice dismiss={() => setError(null)}>{error}</Notice>}
      <div className="member-list">
        {members.map((member) => {
          const editable = manages && (owner || member.role !== "owner");
          return (
            <div className="member-row" key={member.user_id}>
              <div>
                <strong>{member.display_name || member.login_name || "Member"}</strong>
                <span>{member.login_name ? `@${member.login_name}` : member.user_id}</span>
              </div>
              <select disabled={!editable} value={member.role} aria-label={`Role for ${member.display_name || member.user_id}`} onChange={(e) => void change(member, e.target.value as Role)}>
                {choices.map((item) => (
                  <option disabled={!owner && item === "owner"} key={item}>
                    {item}
                  </option>
                ))}
              </select>
              {editable && (
                <button className="icon-button danger" aria-label={`Remove ${member.display_name || member.user_id}`} onClick={() => void remove(member)}>
                  ×
                </button>
              )}
            </div>
          );
        })}
      </div>
      {manages ? (
        <form className="member-form" onSubmit={add}>
          <h3>Add existing user</h3>
          <label>
            User
            <select required value={userId} onChange={(e) => setUserId(e.target.value)}>
              <option value="">Select a user…</option>
              {candidates.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.display_name && user.display_name !== user.login_name ? `${user.display_name} (@${user.login_name})` : `@${user.login_name}`}
                </option>
              ))}
            </select>
          </label>
          <label>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {choices.map((item) => (
                <option disabled={!owner && item === "owner"} key={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <button type="submit">Add member</button>
        </form>
      ) : (
        <p className="muted">Only owners and administrators can manage members.</p>
      )}
    </aside>
  );
}

function ProjectSettings({ session, project, editable, close, saved }: { session: Session; project: Project; editable: boolean; close: () => void; saved: (project: Project) => void }) {
  const configured = project.settings?.quapy_environment?.SAMPLE_SIZE;
  const [sampleSize, setSampleSize] = useState(configured == null ? "" : String(configured));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = sampleSize === "" ? null : Number(sampleSize);
    if (parsed !== null && (!Number.isInteger(parsed) || parsed < 1)) {
      setError("SAMPLE_SIZE must be empty or a positive integer.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await request<Project>(`/api/v1/projects/${project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({
          name: project.name,
          description: project.description,
          settings: { quapy_environment: { SAMPLE_SIZE: parsed } },
        }),
      });
      saved(updated);
    } catch (reason) {
      setError(errorText(reason, "Could not save project settings."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside className="members-panel" aria-label="Project settings">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Project settings</p>
          <h2>QuaPy environment</h2>
        </div>
        <button className="quiet-button" onClick={close}>
          Close
        </button>
      </div>
      <p className="panel-copy">Protocol nodes use their own sample size first, then this project value. If both are empty, the connected collection size is used.</p>
      {error && <Notice dismiss={() => setError(null)}>{error}</Notice>}
      <form className="member-form" onSubmit={submit}>
        <label>
          SAMPLE_SIZE
          <input disabled={!editable} type="number" min="1" step="1" placeholder="Connected collection size" value={sampleSize} onChange={(event) => setSampleSize(event.target.value)} />
        </label>
        {editable && (
          <button disabled={busy} type="submit">
            {busy ? "Saving…" : "Save environment"}
          </button>
        )}
      </form>
    </aside>
  );
}

function Workspace({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [activeId, setActiveId] = useState(session.active_group_id);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [settingsProject, setSettingsProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [groupForm, setGroupForm] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [groupName, setGroupName] = useState("");
  const active = useMemo(() => groups.find((group) => group.id === activeId), [groups, activeId]);
  const reloadGroups = async () => {
    const result = await request<{ items: Group[] }>("/api/v1/groups");
    setGroups(result.items);
    return result.items;
  };
  const reloadMembers = async () => {
    if (!active || active.type === "personal") return;
    const result = await request<{ items: Member[] }>(`/api/v1/groups/${active.id}/members`);
    setMembers(result.items);
  };
  useEffect(() => {
    void reloadGroups().catch((reason) => setError(errorText(reason, "Could not load groups.")));
  }, []);
  useEffect(() => {
    setSelectedProject(null);
    setSettingsProject(null);
    void request<{ items: Project[] }>(`/api/v1/groups/${activeId}/projects`)
      .then((result) => setProjects(result.items))
      .catch((reason) => setError(errorText(reason, "Could not load projects.")));
  }, [activeId]);
  useEffect(() => {
    setMembersOpen(false);
    setMembers([]);
    void reloadMembers().catch((reason) => setError(errorText(reason, "Could not load members.")));
  }, [activeId, active?.type]);
  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!projectName.trim()) return;
    try {
      const project = await request<Project>(`/api/v1/groups/${activeId}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ name: projectName }),
      });
      setProjects((current) => [...current, project].sort((a, b) => a.name.localeCompare(b.name)));
      setProjectName("");
      setCreating(false);
    } catch (reason) {
      setError(errorText(reason, "Could not create project."));
    }
  }
  async function createGroup(event: FormEvent) {
    event.preventDefault();
    const name = groupName.trim();
    if (!name) return;
    try {
      const group = await request<Group>("/api/v1/groups", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ name, slug: slugify(name) }),
      });
      setGroups((current) => [...current, { ...group, role: "owner" as Role }].sort((a, b) => a.name.localeCompare(b.name)));
      setActiveId(group.id);
      setGroupName("");
      setGroupForm(false);
    } catch (reason) {
      setError(errorText(reason, "Could not create group."));
    }
  }
  async function renameGroup() {
    if (!active) return;
    const name = window.prompt("Group name", active.name)?.trim();
    if (!name) return;
    try {
      const updated = await request<Group>(`/api/v1/groups/${active.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ name }),
      });
      setGroups((current) => current.map((item) => (item.id === updated.id ? { ...updated, role: item.role } : item)));
    } catch (reason) {
      setError(errorText(reason, "Could not rename group."));
    }
  }
  async function deleteGroup() {
    if (!active || !window.confirm(`Delete shared group “${active.name}”? Its projects and immutable evidence will be hidden.`)) return;
    try {
      await request<void>(`/api/v1/groups/${active.id}`, {
        method: "DELETE",
        headers: csrf(session),
      });
      const remaining = groups.filter((item) => item.id !== active.id);
      setGroups(remaining);
      setActiveId(remaining.find((item) => item.type === "personal")?.id || remaining[0]?.id || "");
      setSelectedProject(null);
    } catch (reason) {
      setError(errorText(reason, "Could not delete group."));
    }
  }
  async function renameProject(project: Project) {
    const name = window.prompt("Project name", project.name)?.trim();
    if (!name) return;
    try {
      const updated = await request<Project>(`/api/v1/projects/${project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify({ name, description: project.description }),
      });
      setProjects((current) => current.map((item) => (item.id === updated.id ? updated : item)).sort((a, b) => a.name.localeCompare(b.name)));
      if (selectedProject?.id === updated.id) setSelectedProject(updated);
    } catch (reason) {
      setError(errorText(reason, "Could not rename project."));
    }
  }
  async function deleteProject(project: Project) {
    if (!window.confirm(`Delete project “${project.name}”? Workflows, datasets, and runs will be hidden while immutable evidence is retained.`)) return;
    try {
      await request<void>(`/api/v1/projects/${project.id}`, {
        method: "DELETE",
        headers: csrf(session),
      });
      setProjects((current) => current.filter((item) => item.id !== project.id));
      if (selectedProject?.id === project.id) setSelectedProject(null);
    } catch (reason) {
      setError(errorText(reason, "Could not delete project."));
    }
  }
  async function importProject(file: File) {
    try {
      const document = JSON.parse(await file.text()) as object;
      const project = await request<Project>(`/api/v1/groups/${activeId}/projects/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrf(session) },
        body: JSON.stringify(document),
      });
      setProjects((current) => [...current, project].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedProject(project);
    } catch (reason) {
      setError(errorText(reason, "Could not import the project."));
    }
  }
  async function exportProject(project: Project) {
    try {
      const nativeApi = (window as Window & { pywebview?: { api?: NativeExportApi } }).pywebview?.api;
      if (nativeApi) {
        const result = await nativeApi.download_export("project", project.id, false);
        if (!result.saved && !result.cancelled) throw new Error(result.error || "Export failed.");
        return;
      }
      const response = await fetch(`/api/v1/projects/${project.id}/export`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Export failed.");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${project.name}.quapylab.json`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      setError(errorText(reason, "Could not export the project."));
    }
  }
  const canCreate = active?.role !== "viewer";
  const canDeleteProject = session.deployment_mode === "desktop" || active?.role === "owner" || active?.role === "administrator";
  return (
    <main className="workspace-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Visual quantification workbench</p>
          <h1>QuaPy Lab</h1>
        </div>
        {session.deployment_mode === "web" && (
          <div className="topbar-actions">
            <button className="quiet-button" onClick={() => setAccountOpen(true)}>
              Account
            </button>
            <button className="quiet-button" onClick={onLogout}>
              Sign out
            </button>
          </div>
        )}
      </header>
      <section className="workspace">
        <aside className="project-sidebar">
          <div className="sidebar-heading">
            <div>
              <p className="eyebrow">{session.deployment_mode === "desktop" ? "Personal workspace" : "Workspace"}</p>
              <h2>Projects</h2>
            </div>
            {canCreate && (
              <button aria-label="Create project" onClick={() => setCreating(true)}>
                +
              </button>
            )}
          </div>
          {canCreate && (
            <label className="file-button project-import">
              Import project
              <input
                type="file"
                accept=".json,application/json"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void importProject(file);
                  e.target.value = "";
                }}
              />
            </label>
          )}
          {session.deployment_mode === "web" && (
            <div className="group-selector">
              <label>
                Active group
                <select value={activeId} onChange={(e) => setActiveId(e.target.value)}>
                  {groups.map((group) => (
                    <option key={group.id} value={group.id}>
                      {group.name}
                      {group.role ? ` · ${group.role}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {active?.type === "shared" && (
                <button className="link-button" onClick={() => setMembersOpen(true)}>
                  Manage members
                </button>
              )}
              {active?.type === "shared" && (active.role === "owner" || active.role === "administrator") && (
                <button className="link-button" onClick={() => void renameGroup()}>
                  Rename group
                </button>
              )}
              {active?.type === "shared" && active.role === "owner" && (
                <button className="link-button danger-link" onClick={() => void deleteGroup()}>
                  Delete group
                </button>
              )}
              <button className="link-button" onClick={() => setGroupForm(true)}>
                Create shared group
              </button>
            </div>
          )}
          {groupForm && (
            <form className="new-project" onSubmit={createGroup}>
              <input autoFocus aria-label="Group name" placeholder="Group name" value={groupName} onChange={(e) => setGroupName(e.target.value)} />
              <button>Create</button>
            </form>
          )}
          {creating && (
            <form className="new-project" onSubmit={createProject}>
              <input autoFocus aria-label="Project name" placeholder="Project name" value={projectName} onChange={(e) => setProjectName(e.target.value)} />
              <button>Create</button>
            </form>
          )}
          {error && <Notice dismiss={() => setError(null)}>{error}</Notice>}
          <nav aria-label="Projects">
            {projects.length ? (
              projects.map((project) => (
                <div className="project-entry" key={project.id}>
                  <button className="project-row" onClick={() => setSelectedProject(project)}>
                    <strong>{project.name}</strong>
                    <span>{project.description || "Untitled experiment"}</span>
                  </button>
                  {canCreate && (
                    <button className="project-action" aria-label={`Rename ${project.name}`} onClick={() => void renameProject(project)}>
                      Rename
                    </button>
                  )}
                  <button className="project-action" aria-label={`Export ${project.name}`} onClick={() => void exportProject(project)}>
                    Export
                  </button>
                  <button className="project-action" aria-label={`Settings for ${project.name}`} onClick={() => setSettingsProject(project)}>
                    Settings
                  </button>
                  {canDeleteProject && (
                    <button className="project-action danger" aria-label={`Delete ${project.name}`} onClick={() => void deleteProject(project)}>
                      Delete
                    </button>
                  )}
                </div>
              ))
            ) : (
              <p className="empty-state">{canCreate ? "Create a project to start an experiment." : "No projects are available in this group."}</p>
            )}
          </nav>
        </aside>
        {selectedProject ? (
          <WorkflowEditor projectId={selectedProject.id} editable={canCreate} session={session} />
        ) : (
          <section className="welcome-panel">
            <p className="eyebrow">{active?.name || "Workspace"}</p>
            <h2>Build a reproducible workflow</h2>
            <p>Select a project to edit a versioned workflow.</p>
          </section>
        )}
        {accountOpen && <AccountPanel session={session} close={() => setAccountOpen(false)} />}
        {membersOpen && active && <Members session={session} group={active} members={members} close={() => setMembersOpen(false)} reload={reloadMembers} />}
        {settingsProject && (
          <ProjectSettings
            session={session}
            project={settingsProject}
            editable={canCreate}
            close={() => setSettingsProject(null)}
            saved={(updated) => {
              setProjects((current) => current.map((item) => (item.id === updated.id ? updated : item)));
              if (selectedProject?.id === updated.id) setSelectedProject(updated);
              setSettingsProject(updated);
            }}
          />
        )}
      </section>
    </main>
  );
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    void request<Session>("/api/v1/auth/session")
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setLoading(false));
  }, []);
  if (loading) return <main className="loading">Loading QuaPy Lab…</main>;
  if (!session) return <Login onAuthenticated={setSession} />;
  return (
    <Workspace
      session={session}
      onLogout={() => {
        void request("/api/v1/auth/logout", {
          method: "POST",
          headers: csrf(session),
        }).finally(() => setSession(null));
      }}
    />
  );
}
