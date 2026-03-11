from telegram_bot.services.media_manager import validate_torrent_files


class DummyFiles:
    def __init__(self, files):
        self._files = files

    def num_files(self):
        return len(self._files)

    def file_path(self, index):
        return self._files[index][0]

    def file_size(self, index):
        return self._files[index][1]


class DummyTorrent:
    def __init__(self, files):
        self._files = DummyFiles(files)

    def files(self):
        return self._files


def test_torrent_service_contracts_validates_files():
    ti = DummyTorrent([("Movie.mkv", 20 * 1024 * 1024)])
    assert validate_torrent_files(ti) is None

    ti_invalid = DummyTorrent([("Movie.avi", 20 * 1024 * 1024)])
    assert "unsupported" in (validate_torrent_files(ti_invalid) or "")
