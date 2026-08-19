

def test_slash_refund_is_accepted_alongside_the_bare_word():
    """`/refund` is the canonical spelling — the only one BotFather can register,
    and the one that stops the manual apologising for "no leading /". The bare
    word keeps working because it shipped that way."""
    from kanakko.commands.refund import _is_refund

    assert _is_refund("/refund 500")
    assert _is_refund("/refund@kanakko_bot 500")
    assert _is_refund("refund 500")  # the original spelling still answers
    assert not _is_refund("got refund 500")  # only as the opening word
    assert not _is_refund("refunded 500")
