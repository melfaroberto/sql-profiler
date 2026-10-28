"""The Session & Tagging Manager: groups captured queries under a UI action ("User Login", "Save Invoice"),
and under sub-actions of it, at any depth ("Customers" → "Search" → "Advanced filters")."""
import threading
import time


class SessionManager:
    def __init__(self, storage, on_change=None):
        self.storage = storage
        self.on_change = on_change or (lambda session, event: None)
        self._lock = threading.Lock()
        # the sessions being recorded, outermost first; the queries go to the last one. It starts with a
        # top-level session (Start recording) or with a sub-session added to a closed session.
        self.stack = []

    @property
    def current(self):
        """The outermost session being recorded, or None."""
        return self.stack[0] if self.stack else None

    def start_recording(self, action_name):
        """Starts a new session; whatever is being recorded is closed first."""
        label = (action_name or "").strip() or time.strftime("Action %Y-%m-%d %H:%M:%S")
        with self._lock:
            closed = self._close_locked(0)
            session = self.storage.create_session(label)
            self.stack = [session]
        for s in closed:
            self.on_change(s, "stopped")
        self.on_change(session, "started")
        return session

    def stop_recording(self):
        """Stops everything being recorded; returns the outermost session."""
        with self._lock:
            closed = self._close_locked(0)
        for s in closed:
            self.on_change(s, "stopped")
        return closed[-1] if closed else None

    def start_sub(self, action_name, parent_id=None):
        """Starts a sub-session inside the innermost session being recorded or, when nothing is recorded,
        inside parent_id (a closed session: the sub-action is added to it later)."""
        with self._lock:
            if self.stack:
                parent = self.stack[-1]
            else:
                parent = self.storage.get_session(int(parent_id)) if parent_id else None
                if parent is None:
                    raise ValueError("start recording an action first, or select the session to add it to")
            label = (action_name or "").strip() or time.strftime("Sub-action %H:%M:%S")
            sub = self.storage.create_session(label, parent_id=parent["id"])
            self.stack.append(sub)
        self.on_change(sub, "started")
        return sub

    def stop_sub(self):
        """Stops the innermost sub-session; the queries go back to the session that contains it."""
        with self._lock:
            if not self.stack or (len(self.stack) == 1 and not self.stack[0].get("parent_id")):
                return None
            closed = self._close_locked(len(self.stack) - 1)
        for s in closed:
            self.on_change(s, "stopped")
        return closed[0] if closed else None

    def _close_locked(self, depth):
        """Closes the sessions of the stack from depth inwards; returns the sessions that changed, innermost first."""
        changed = []
        while len(self.stack) > depth:
            changed.append(self.storage.end_session(self.stack.pop()["id"]))
        if changed and not self.stack and changed[-1].get("parent_id"):
            # sub-actions added to a closed session extend it and the sessions that contain it
            end, pid = changed[-1]["ended_at"], changed[-1]["parent_id"]
            while pid:
                parent = self.storage.get_session(pid)
                if parent is None:
                    break
                if (parent["ended_at"] or 0) < end:
                    changed.append(self.storage.end_session(parent["id"], end))
                pid = parent.get("parent_id")
        return changed

    def session_for(self, ts):
        """Session id for a query that started at ts (None when not recording)."""
        with self._lock:
            stack = list(self.stack)
        for s in reversed(stack):
            if ts >= s["started_at"] - 0.001:
                return s["id"]
        return None

    def group(self, label, query_ids, parent_id=None):
        """Creates a closed session from queries captured outside a recording (forgot to press Record), or,
        with parent_id, a sub-session of that session (e.g. from some of its queries)."""
        rows = [self.storage.get_query(i) for i in query_ids]
        rows = [r for r in rows if r]
        if not rows:
            return None
        if parent_id is not None and self.storage.get_session(int(parent_id)) is None:
            raise ValueError(f"session {parent_id} not found")
        session = self.storage.create_session(label.strip() or "Grouped queries",
                                              min(r["ts"] for r in rows), max(r["ts"] for r in rows),
                                              parent_id=int(parent_id) if parent_id is not None else None)
        self.storage.assign_queries([r["id"] for r in rows], session["id"])
        session = self.storage.get_session(session["id"])
        self.on_change(session, "created")
        return session

    def path(self, session_id):
        """The session and the sessions that contain it, outermost first."""
        out = []
        while session_id:
            s = self.storage.get_session(session_id)
            if s is None:
                break
            out.insert(0, s)
            session_id = s.get("parent_id")
        return out

    def status(self):
        """{"recording", "path": the innermost session being recorded and its ancestors (outermost first),
        "recording_ids": the sessions of the path that are being recorded, "session": path[0],
        "sub": the innermost session when it is not path[0]}"""
        with self._lock:
            stack = list(self.stack)
        if not stack:
            return {"recording": False}
        path = self.path(stack[-1]["id"])
        return {"recording": True, "path": path, "recording_ids": [s["id"] for s in stack],
                "session": path[0], "sub": path[-1] if len(path) > 1 else None}
