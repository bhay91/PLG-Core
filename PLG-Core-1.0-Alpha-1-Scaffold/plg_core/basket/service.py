class BasketService:
    def __init__(self):
        self._items = []

    def add(self, item):
        self._items.append(item)

    def all(self):
        return list(self._items)
