import pytest
import media.yuv_range_matrix as mod

CASES = [
    ("read_actual_range", (1,)),
    ("matrix_default_hd", (1,)),
    ("unify_chroma_site", (1,)),
    ("float_convert", (1,)),
    ("sei_matrix", (1,)),
    ("decode", (1,)),
    ("convert", (1,)),
    ("present", (1,)),
    ("transcode", (1,)),
    ("range_check", (1,)),
    ("validate_color", (1,)),
    ("metrics", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("determinism", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
