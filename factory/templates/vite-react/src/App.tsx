import { useEffect, useState } from 'react'
import './index.css'

type Todo = { id: string; text: string; done: boolean }

const STORAGE_KEY = 'factory-todo'

function load(): Todo[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return JSON.parse(raw)
  } catch {
    /* ignore */
  }
  return [
    { id: 'seed-1', text: 'Ship the todo app', done: true },
    { id: 'seed-2', text: 'Run it in the sandbox', done: true },
    { id: 'seed-3', text: 'Deploy to Cloudflare Pages', done: false },
  ]
}

export default function App() {
  const [todos, setTodos] = useState<Todo[]>(load)
  const [draft, setDraft] = useState('')
  const [filter, setFilter] = useState<'all' | 'open' | 'done'>('all')

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(todos))
  }, [todos])

  function add(e: React.FormEvent) {
    e.preventDefault()
    const text = draft.trim()
    if (!text) return
    setTodos((prev) => [...prev, { id: crypto.randomUUID(), text, done: false }])
    setDraft('')
  }

  function toggle(id: string) {
    setTodos((prev) => prev.map((t) => (t.id === id ? { ...t, done: !t.done } : t)))
  }

  function remove(id: string) {
    setTodos((prev) => prev.filter((t) => t.id !== id))
  }

  const visible = todos.filter((t) =>
    filter === 'all' ? true : filter === 'open' ? !t.done : t.done,
  )
  const left = todos.filter((t) => !t.done).length

  return (
    <div className="app">
      <header>
        <h1>TODO</h1>
        <p>built on the factory platform · live from the sandbox</p>
      </header>

      <form className="add" onSubmit={add}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="what needs doing?"
          aria-label="new todo"
        />
        <button type="submit" disabled={!draft.trim()}>
          add
        </button>
      </form>

      <nav className="filters">
        {(['all', 'open', 'done'] as const).map((f) => (
          <button
            key={f}
            className={filter === f ? 'on' : ''}
            onClick={() => setFilter(f)}
          >
            {f} {f === 'all' ? todos.length : f === 'open' ? left : todos.length - left}
          </button>
        ))}
      </nav>

      <ul className="todos">
        {visible.map((t) => (
          <li key={t.id} className={t.done ? 'done' : ''}>
            <input
              type="checkbox"
              checked={t.done}
              onChange={() => toggle(t.id)}
              aria-label={`toggle ${t.text}`}
            />
            <span className="text">{t.text}</span>
            <button className="x" onClick={() => remove(t.id)} aria-label={`delete ${t.text}`}>
              ✕
            </button>
          </li>
        ))}
        {visible.length === 0 && <li className="empty">nothing here — add a todo above</li>}
      </ul>

      <footer>{left} left · {todos.length} total</footer>
    </div>
  )
}
