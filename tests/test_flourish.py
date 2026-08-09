from wren.flourish import flourish, EMOTES

def test_flourish_appends_a_space_and_an_emote():
    result = flourish("Saved.")
    assert result.startswith("Saved. ")
    trailing = result.rsplit(" ", 1)[1]
    assert trailing in EMOTES

def test_flourish_preserves_original_text_exactly():
    result = flourish("Cancelled: check the oven.")
    prefix = result.rsplit(" ", 1)[0]
    assert prefix == "Cancelled: check the oven."

def test_flourish_uses_more_than_one_emote_across_many_calls():
    seen = {flourish("x").rsplit(" ", 1)[1] for _ in range(200)}
    assert len(seen) > 1
