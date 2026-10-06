import { FormEvent, useEffect, useState } from 'react'

type Session = {
  authenticated: boolean
  deployment_mode: 'desktop' | 'web'
  user_id: string
  active_group_id: string
  csrf_token: string | null
}

type Project = { id: string; name: string; description: string }
type ApiError = { error?: { message?: string } }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init })
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError
    throw new Error(body.error?.message ?? 'The request could not be completed.')
  }
  return response.json() as Promise<T>
}

function Login({ onAuthenticated }: { onAuthenticated: (session: Session) => void }) {
  const [loginName, setLoginName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await request('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login_name: loginName, password }) })
      onAuthenticated(await request<Session>('/api/v1/auth/session'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Login failed.')
    } finally {
      setBusy(false)
    }
  }

  return <main className="auth-shell"><form className="login-card" onSubmit={submit}><p className="eyebrow">QuaPy Lab</p><h1>Welcome back</h1><p>Sign in to your reproducible quantification workspace.</p><label>Login name<input autoComplete="username" value={loginName} onChange={(event) => setLoginName(event.target.value)} /></label><label>Password<input autoComplete="current-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>{error && <p className="form-error" role="alert">{error}</p>}<button disabled={busy} type="submit">{busy ? 'Signing in…' : 'Sign in'}</button></form></main>
}

function Workspace({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [projectName, setProjectName] = useState('')

  useEffect(() => {
    void request<{ items: Project[] }>(`/api/v1/groups/${session.active_group_id}/projects`)
      .then((result) => setProjects(result.items))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Could not load projects.'))
  }, [session.active_group_id])

  async function createProject(event: FormEvent) {
    event.preventDefault()
    if (!projectName.trim()) return
    try {
      const project = await request<Project>(`/api/v1/groups/${session.active_group_id}/projects`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(session.csrf_token ? { 'X-CSRF-Token': session.csrf_token } : {}) },
        body: JSON.stringify({ name: projectName }),
      })
      setProjects((current) => [...current, project].sort((left, right) => left.name.localeCompare(right.name)))
      setProjectName('')
      setCreating(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not create project.')
    }
  }

  return <main className="workspace-shell"><header className="topbar"><div><p className="eyebrow">Visual quantification workbench</p><h1>QuaPy Lab</h1></div>{session.deployment_mode === 'web' && <button className="quiet-button" onClick={onLogout}>Sign out</button>}</header><section className="workspace"><aside className="project-sidebar"><div className="sidebar-heading"><div><p className="eyebrow">{session.deployment_mode === 'desktop' ? 'Personal workspace' : 'Active group'}</p><h2>Projects</h2></div><button aria-label="Create project" onClick={() => setCreating(true)}>+</button></div>{creating && <form className="new-project" onSubmit={createProject}><input autoFocus aria-label="Project name" placeholder="Project name" value={projectName} onChange={(event) => setProjectName(event.target.value)} /><button type="submit">Create</button></form>}{error && <p className="form-error" role="alert">{error}</p>}<nav aria-label="Projects">{projects.length ? projects.map((project) => <button className="project-row" key={project.id}><strong>{project.name}</strong><span>{project.description || 'Untitled experiment'}</span></button>) : <p className="empty-state">Create a project to start an experiment.</p>}</nav></aside><section className="welcome-panel"><p className="eyebrow">Start here</p><h2>Build a reproducible workflow</h2><p>Projects hold versioned workflow graphs, datasets, runs, and resulting model artifacts.</p><div className="workflow-placeholder"><span>Dataset</span><i>→</i><span>Classifier</span><i>→</i><span>Quantifier</span><i>→</i><span>Evaluation</span></div></section></section></main>
}

export function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => { void request<Session>('/api/v1/auth/session').then(setSession).catch(() => setSession(null)).finally(() => setLoading(false)) }, [])
  if (loading) return <main className="loading">Loading QuaPy Lab…</main>
  if (!session) return <Login onAuthenticated={setSession} />
  return <Workspace session={session} onLogout={() => { void request('/api/v1/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token ?? '' } }).finally(() => setSession(null)) }} />
}
