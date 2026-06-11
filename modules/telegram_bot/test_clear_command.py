import module


def test_clear_command_detection():
    assert module.is_clear_command({"text": "/clear"}, {})
    assert module.is_clear_command({"text": "/clear@mybot now"}, {})
    assert not module.is_clear_command({"text": "/start"}, {})
    assert not module.is_clear_command({"text": "/clear"}, {"clear_command_enabled": False})


def test_clear_archives_current_conversation_and_rotates_active_id():
    saved = []
    sent = []
    old_load = module.load_convo
    old_save = module.save_convo
    old_send = module.tg_send_message
    old_save_state = module.save_state
    try:
        module.load_convo = lambda config, target, convo_id: {
            "id": convo_id,
            "title": "TELEGRAM:@Chigxc",
            "messages": [{"role": "user", "content": "alte frage"}],
        }
        module.save_convo = lambda config, target, convo_id, title, messages, extra=None: saved.append(
            {"target": target, "convo_id": convo_id, "title": title, "messages": messages, "extra": extra}
        )
        module.tg_send_message = lambda config, chat_id, text: sent.append((chat_id, text))
        module.save_state = lambda config, state: None

        state = {"chats": {"123": {"last_inbound": {"answered": False}}}}
        message = {
            "text": "/clear",
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 7, "username": "Chigxc"},
        }
        assert module.handle_clear_command(message, {"target_modul_id": "chat.test"}, state)

        chat_state = state["chats"]["123"]
        assert chat_state["active_convo_id"].startswith("telegram_123_")
        assert chat_state["active_convo_id"] != "telegram_123"
        assert "last_inbound" not in chat_state
        assert saved[0]["convo_id"] == "telegram_123"
        assert saved[0]["extra"]["archived"] is True
        assert saved[0]["extra"]["closed_by"] == "telegram:/clear"
        assert saved[0]["extra"]["next_convo_id"] == chat_state["active_convo_id"]
        assert "Archiv" in saved[0]["title"]
        assert "Kontext geleert" in sent[0][1]
    finally:
        module.load_convo = old_load
        module.save_convo = old_save
        module.tg_send_message = old_send
        module.save_state = old_save_state


def test_remember_uses_rotated_conversation_id():
    state = {"chats": {"123": {"active_convo_id": "telegram_123_20260514T120000Z_abcd1234"}}}
    message = {"message_id": 1, "chat": {"id": 123}, "from": {"id": 7}}
    module.remember_inbound_message(state, message, "hi", False, False, {"target_modul_id": "chat.test"})
    assert state["chats"]["123"]["last_inbound"]["convo_id"] == "telegram_123_20260514T120000Z_abcd1234"


def test_telegram_fast_reply_handles_simple_greetings():
    assert module.telegram_fast_reply("hi") == "hi"
    assert module.telegram_fast_reply("Hi!") == "hi"
    assert module.telegram_fast_reply("hallo") == "hi"
    assert module.telegram_fast_reply("hi", was_voice=True) == ""
    assert module.telegram_fast_reply("hi", requested_voice=True) == ""
    assert module.telegram_fast_reply("hi", has_image=True) == ""
    assert module.telegram_fast_reply("hi was geht") == ""


def test_voice_reply_chunks_do_not_silently_cut_after_first_chunk():
    text = " ".join(f"Satz {idx} mit etwas Inhalt." for idx in range(80))
    chunks = module.voice_reply_chunks(text, 180, 20)
    assert len(chunks) > 1
    assert "Satz 0" in chunks[0]
    assert "Satz 79" in " ".join(chunks)
    assert all(len(chunk) <= 180 for chunk in chunks)


def test_voice_reply_chunks_mark_limit_when_too_many_parts():
    text = " ".join(f"Satz {idx} mit etwas Inhalt." for idx in range(80))
    chunks = module.voice_reply_chunks(text, 180, 2)
    assert len(chunks) == 2
    assert chunks[-1].endswith("Rest steht als Text.")


def test_deliver_reply_sends_multiple_voice_chunks():
    synthesized = []
    sent_audio = []
    sent_text = []
    old_enhance = module.enhance_voice_output
    old_synthesize = module.synthesize_tts
    old_send_audio = module.send_audio_reply
    old_send_text = module.tg_send_message
    try:
        module.enhance_voice_output = lambda text, config: text
        module.synthesize_tts = lambda text, config: synthesized.append(text) or "/tmp/telegram-test-voice.mp3"
        module.send_audio_reply = lambda config, chat_id, path, caption="": sent_audio.append((chat_id, caption))
        module.tg_send_message = lambda config, chat_id, text: sent_text.append((chat_id, text))
        text = " ".join(f"Satz {idx} mit etwas Inhalt." for idx in range(40))

        delivered = module.deliver_reply(
            {"max_tts_chars": 160, "max_tts_chunks": 10, "send_text_replies": False},
            "123",
            text,
            False,
            True,
        )

        assert delivered is True
        assert len(synthesized) > 1
        assert len(sent_audio) == len(synthesized)
        assert sent_text == []
        assert sent_audio[0][1].startswith("Voice-Antwort 1/")
    finally:
        module.enhance_voice_output = old_enhance
        module.synthesize_tts = old_synthesize
        module.send_audio_reply = old_send_audio
        module.tg_send_message = old_send_text


def test_voice_output_summary_removes_sources_and_limits_audio_text():
    text = """
    [42 Aufgabe(n) erstellt, 1 Tool-Fehler behandelt]
    # Xi-Trump-Taiwan DeepDive
    Aktueller Stand: Xi hat Trump wegen Taiwan deutlich gewarnt. Die Lage ist angespannt,
    weil Taiwan, Chips, Nvidia und Exportkontrollen zusammenhaengen.

    <quellen>
    https://example.com/source
    source_links: lots of English source metadata
    </quellen>
    """

    audio = module.enhance_voice_output(
        text,
        {"voice_output_enhancer_enabled": False, "voice_output_max_chars": 260},
    )

    lowered = audio.lower()
    assert len(audio) <= 260
    assert "http" not in lowered
    assert "source_links" not in lowered
    assert "quellen" not in lowered
    assert "xi" in lowered
    assert "taiwan" in lowered


def test_deliver_reply_uses_voice_enhancer_only_for_audio():
    enhanced = []
    synthesized = []
    sent_audio = []
    sent_text = []
    old_enhance = module.enhance_voice_output
    old_synthesize = module.synthesize_tts
    old_send_audio = module.send_audio_reply
    old_send_text = module.tg_send_message
    try:
        module.enhance_voice_output = lambda text, config: enhanced.append(text) or "Kurze deutsche Audiofassung."
        module.synthesize_tts = lambda text, config: synthesized.append(text) or "/tmp/telegram-test-voice.mp3"
        module.send_audio_reply = lambda config, chat_id, path, caption="": sent_audio.append((chat_id, caption))
        module.tg_send_message = lambda config, chat_id, text: sent_text.append((chat_id, text))
        full_text = "VOLLER TEXTBERICHT mit Quellen: https://example.com und vielen Details."

        delivered = module.deliver_reply(
            {"max_tts_chars": 1000, "max_tts_chunks": 1, "send_text_replies": True},
            "123",
            full_text,
            True,
            False,
        )

        assert delivered is True
        assert enhanced == [full_text]
        assert synthesized == ["Kurze deutsche Audiofassung."]
        assert sent_audio == [("123", "Voice-Antwort")]
        assert sent_text == [("123", full_text)]
    finally:
        module.enhance_voice_output = old_enhance
        module.synthesize_tts = old_synthesize
        module.send_audio_reply = old_send_audio
        module.tg_send_message = old_send_text


def test_prepare_tts_text_normalizes_ascii_german_orthography():
    text = "Der US-Praesident koennte fuer die Laender aeusserst gefaehrlich werden."

    normalized = module.prepare_tts_text(text, {"tts_german_orthography": True})

    assert "Präsident" in normalized
    assert "könnte" in normalized
    assert "für" in normalized
    assert "Länder" in normalized
    assert "äußerst" in normalized
    assert "gefährlich" in normalized


def test_split_text_prefers_sentence_boundary_over_last_space():
    first = "A" * 80 + ". "
    second = "B" * 105
    text = first + second + "."

    chunks = module.split_text(text, 120)

    assert len(chunks) == 2
    assert chunks[0].endswith(".")
    assert chunks[1].startswith("B")


def test_progress_ping_is_disabled_by_default_and_custom_only():
    sent = []
    old_send = module.tg_send_message
    try:
        module.tg_send_message = lambda config, chat_id, text: sent.append((str(chat_id), text))
        state = {}
        message = {"message_id": 7, "chat": {"id": 123}, "from": {"id": 7}}
        module.remember_inbound_message(state, message, "such kurz nach uap news", False, False, {"target_modul_id": "chat.test"})

        assert not module.maybe_send_progress_ping({}, state, 123, "such kurz nach uap news", False)
        assert sent == []
        assert module.maybe_send_progress_ping(
            {"progress_ping_enabled": True, "progress_ping_text": "Ich pruefe das."},
            state,
            123,
            "such kurz nach uap news",
            False,
        )
        assert not module.maybe_send_progress_ping(
            {"progress_ping_enabled": True, "progress_ping_text": "Ich pruefe das."},
            state,
            123,
            "such kurz nach uap news",
            False,
        )
        assert sent == [("123", "Ich pruefe das.")]
        assert state["chats"]["123"]["last_inbound"]["progress_sent"] is True
    finally:
        module.tg_send_message = old_send


def test_failed_delivery_keeps_reply_ready_for_watchdog():
    state = {}
    message = {"message_id": 8, "chat": {"id": 123}, "from": {"id": 7}}
    module.remember_inbound_message(state, message, "hi", False, False, {"target_modul_id": "chat.test"})

    module.mark_inbound_answered(state, 123, "fertige antwort", delivered=False)

    pending = state["chats"]["123"]["last_inbound"]
    assert pending["answered"] is False
    assert pending["reply_ready"] is True
    assert pending["reply_text"] == "fertige antwort"
    assert pending["delivery_attempts"] == 1


def test_watchdog_redelivers_ready_reply_before_rerunning_llm():
    delivered = []
    old_deliver = module.deliver_reply
    old_save_state = module.save_state
    try:
        module.deliver_reply = lambda config, chat_id, text, was_voice, requested_voice: delivered.append(
            (str(chat_id), text, was_voice, requested_voice)
        ) or True
        module.save_state = lambda config, state: None
        now = module.time.time()
        state = {
            "chats": {
                "123": {
                    "last_inbound": {
                        "message_id": 9,
                        "chat_id": "123",
                        "text": "hi",
                        "answered": False,
                        "reply_ready": True,
                        "reply_text": "fertige antwort",
                        "was_voice": False,
                        "requested_voice": False,
                        "created_ts": int(now) - 30,
                        "last_attempt_ts": int(now) - 30,
                        "last_delivery_attempt_ts": int(now) - 30,
                        "delivery_attempts": 0,
                    }
                }
            }
        }

        result = module.retry_unanswered_messages(
            {"reply_watchdog_enabled": True, "reply_delivery_check_delay_s": 0},
            state,
        )

        assert result["recovered"] == 1
        assert delivered == [("123", "fertige antwort", False, False)]
        pending = state["chats"]["123"]["last_inbound"]
        assert pending["answered"] is True
        assert pending["reply_ready"] is False
        assert "reply_text" not in pending
    finally:
        module.deliver_reply = old_deliver
        module.save_state = old_save_state


def test_watchdog_delivers_finished_webchat_reply_even_after_max_attempts():
    delivered = []
    old_latest = module.latest_assistant_reply
    old_active = module.llm_task_active
    old_deliver = module.deliver_reply
    old_save_state = module.save_state
    try:
        module.latest_assistant_reply = lambda config, target, convo_id: "fertige antwort aus webchat"
        module.llm_task_active = lambda config, target, convo_id: False
        module.deliver_reply = lambda config, chat_id, text, was_voice, requested_voice: delivered.append(
            (str(chat_id), text, was_voice, requested_voice)
        ) or True
        module.save_state = lambda config, state: None
        now = int(module.time.time())
        state = {
            "chats": {
                "123": {
                    "active_convo_id": "telegram_123_test",
                    "last_inbound": {
                        "message_id": 10,
                        "chat_id": "123",
                        "text": "lange recherche",
                        "answered": False,
                        "was_voice": False,
                        "requested_voice": False,
                        "target_modul_id": "chat.test",
                        "convo_id": "telegram_123_test",
                        "created_ts": now - 120,
                        "last_attempt_ts": now - 120,
                        "last_retry_ts": now - 120,
                        "attempts": 99,
                    }
                }
            }
        }

        result = module.retry_unanswered_messages(
            {
                "reply_watchdog_enabled": True,
                "reply_watchdog_delay_s": 0,
                "reply_watchdog_max_attempts": 3,
            },
            state,
        )

        assert result["recovered"] == 1
        assert delivered == [("123", "fertige antwort aus webchat", False, False)]
        assert state["chats"]["123"]["last_inbound"]["answered"] is True
    finally:
        module.latest_assistant_reply = old_latest
        module.llm_task_active = old_active
        module.deliver_reply = old_deliver
        module.save_state = old_save_state


def test_telegram_user_content_wraps_pasted_webpage_data():
    pasted = "\n".join(
        [
            "Ryzen 5",
            "5600T(100-000001584)",
            "65W",
            "Vermeer",
            "P10.08",
            "If you need to update BIOS, please click here.",
            "The specification is subject to change without notice.",
            "Privacy Policy",
            "Terms of Use",
        ]
    )
    wrapped = module.telegram_user_content(pasted)
    assert "NUTZER-NACHRICHT" in wrapped
    assert "beschuldige den Nutzer nicht" in wrapped
    assert pasted in wrapped


def test_telegram_live_content_keeps_casual_chat_out_of_research_mode():
    wrapped = module.telegram_live_content("ich glaub etwas stimmt nicht")
    lowered = wrapped.lower()
    assert "TELEGRAM_CHAT_MODE" in wrapped
    assert "ich glaub etwas stimmt nicht" in wrapped
    assert "recherche" not in lowered
    assert "deepdive" not in lowered
    assert "tool" not in lowered
    assert "ich glaub etwas stimmt nicht" in wrapped


if __name__ == "__main__":
    test_clear_command_detection()
    test_clear_archives_current_conversation_and_rotates_active_id()
    test_remember_uses_rotated_conversation_id()
    test_telegram_fast_reply_handles_simple_greetings()
    test_voice_reply_chunks_do_not_silently_cut_after_first_chunk()
    test_voice_reply_chunks_mark_limit_when_too_many_parts()
    test_deliver_reply_sends_multiple_voice_chunks()
    test_voice_output_summary_removes_sources_and_limits_audio_text()
    test_deliver_reply_uses_voice_enhancer_only_for_audio()
    test_prepare_tts_text_normalizes_ascii_german_orthography()
    test_split_text_prefers_sentence_boundary_over_last_space()
    test_progress_ping_is_disabled_by_default_and_custom_only()
    test_failed_delivery_keeps_reply_ready_for_watchdog()
    test_watchdog_redelivers_ready_reply_before_rerunning_llm()
    test_watchdog_delivers_finished_webchat_reply_even_after_max_attempts()
    test_telegram_user_content_wraps_pasted_webpage_data()
    test_telegram_live_content_keeps_casual_chat_out_of_research_mode()
    print("telegram clear tests ok")
