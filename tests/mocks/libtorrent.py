class torrent_status:
    pass


class torrent_info:
    def __init__(self, *args, **kwargs):
        pass

    def name(self):
        return "mock_torrent"

    def files(self):
        return file_storage()

    def total_size(self):
        return 0


class file_storage:
    def num_files(self):
        return 0

    def file_path(self, index):
        return ""

    def file_size(self, index):
        return 0


class session:
    delete_files = 1

    def __init__(self, *args, **kwargs):
        pass

    def listen_on(self, *args, **kwargs):
        pass

    def start_dht(self):
        pass

    def add_dht_router(self, *args):
        pass

    def add_torrent(self, *args, **kwargs):
        pass


class storage_mode_t:
    storage_mode_sparse = 1


def parse_magnet_uri(uri):
    return add_torrent_params()


class add_torrent_params:
    storage_mode = 0
    save_path = ""


def bencode(data):
    return b""


class create_torrent:
    def __init__(self, ti):
        pass

    def generate(self):
        return {}


class torrent_flags:
    paused = 1
