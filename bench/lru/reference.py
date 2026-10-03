from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be 1 or more")
        self.capacity, self.items = capacity, OrderedDict()

    def get(self, key, default=None):
        if key not in self.items:
            return default
        self.items.move_to_end(key)
        return self.items[key]

    def put(self, key, value):
        if key in self.items:
            self.items.move_to_end(key)
        elif len(self.items) >= self.capacity:
            self.items.popitem(last=False)
        self.items[key] = value

    def __len__(self):
        return len(self.items)
