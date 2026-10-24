"""Shared fakes for the prototype app tests (no tests of its own)."""


class SpyPanel:
    def __init__(self):
        self.pushes = []
        self.deletes = []

    def push_app(self, name, payload):
        self.pushes.append((name, payload))

    def delete_pushed_app(self, name):
        self.deletes.append(name)
        return True


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
