#!/usr/bin/env python3
"""
Patch Handy source code to add phrase substitution feature.

Runs inside the GitHub Actions workflow before `bun run tauri build`.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_RS = ROOT / "src-tauri" / "src" / "settings.rs"
ACTIONS_RS = ROOT / "src-tauri" / "src" / "actions.rs"
TAURI_CONF = ROOT / "src-tauri" / "tauri.conf.json"


PHRASE_STRUCT = r'''
#[derive(Serialize, Deserialize, Debug, Clone, Type)]
pub struct PhraseSubstitution {
    pub pattern: String,
    pub replacement: String,
}
'''

DEFAULT_PHRASE_SUBS_FN = r'''
fn default_phrase_substitutions() -> Vec<PhraseSubstitution> {
    vec![
        PhraseSubstitution { pattern: "три восклицательных знака".to_string(), replacement: "!!!".to_string() },
        PhraseSubstitution { pattern: "два восклицательных знака".to_string(), replacement: "!!".to_string() },
        PhraseSubstitution { pattern: "восклицательный знак".to_string(), replacement: "!".to_string() },
        PhraseSubstitution { pattern: "многоточие".to_string(), replacement: "…".to_string() },
        PhraseSubstitution { pattern: "тире".to_string(), replacement: "—".to_string() },
    ]
}
'''


def patch_settings_rs():
    src = SETTINGS_RS.read_text(encoding="utf-8")
    if "PhraseSubstitution" in src:
        print("settings.rs already patched - skipping")
        return

    m = re.search(r"(pub struct LLMPrompt \{[^}]*\})", src, re.DOTALL)
    if not m:
        raise RuntimeError("Couldn't locate LLMPrompt struct in settings.rs")
    src = src[:m.end()] + "\n" + PHRASE_STRUCT + src[m.end():]

    m = re.search(r"^([ \t]*)pub custom_words:\s*Vec<String>,\s*$", src, re.MULTILINE)
    if not m:
        raise RuntimeError("Couldn't find custom_words field in AppSettings")
    indent = m.group(1)
    new_field_block = (
        f'{indent}#[serde(default = "default_phrase_substitutions")]\n'
        f'{indent}pub phrase_substitutions: Vec<PhraseSubstitution>,\n'
    )
    src = src[:m.end()] + "\n" + new_field_block + src[m.end():]

    m = re.search(r"^fn ensure_post_process_defaults\(", src, re.MULTILINE)
    if not m:
        m = re.search(r"^fn default_typing_tool\(\)", src, re.MULTILINE)
    if not m:
        raise RuntimeError("Couldn't find anchor for default_phrase_substitutions fn")
    src = src[:m.start()] + DEFAULT_PHRASE_SUBS_FN + "\n" + src[m.start():]

    m = re.search(r"^([ \t]*)custom_words:\s*Vec::new\(\),\s*$", src, re.MULTILINE)
    if not m:
        raise RuntimeError("Couldn't find custom_words init in get_default_settings()")
    indent = m.group(1)
    insert_text = f"\n{indent}phrase_substitutions: default_phrase_substitutions(),"
    src = src[:m.end()] + insert_text + src[m.end():]

    SETTINGS_RS.write_text(src, encoding="utf-8")
    print("settings.rs patched")


APPLY_FN = r'''
fn apply_phrase_substitutions(text: &str, substitutions: &[crate::settings::PhraseSubstitution]) -> String {
    if substitutions.is_empty() {
        return text.to_string();
    }
    let mut result = text.to_string();
    let mut sorted: Vec<&crate::settings::PhraseSubstitution> = substitutions.iter().collect();
    sorted.sort_by_key(|s| std::cmp::Reverse(s.pattern.chars().count()));

    for sub in sorted {
        if sub.pattern.is_empty() {
            continue;
        }
        loop {
            let lower_result = result.to_lowercase();
            let lower_pattern = sub.pattern.to_lowercase();
            if let Some(idx) = lower_result.find(&lower_pattern) {
                let target_chars = lower_result[..idx].chars().count();
                let mut byte_start = result.len();
                let mut chars_seen = 0usize;
                for (i, _) in result.char_indices() {
                    if chars_seen == target_chars {
                        byte_start = i;
                        break;
                    }
                    chars_seen += 1;
                }
                let pattern_chars = sub.pattern.chars().count();
                let mut byte_end = result.len();
                let mut counted = 0usize;
                for (i, _) in result[byte_start..].char_indices() {
                    if counted == pattern_chars {
                        byte_end = byte_start + i;
                        break;
                    }
                    counted += 1;
                }
                result.replace_range(byte_start..byte_end, &sub.replacement);
            } else {
                break;
            }
        }
    }
    while result.contains("  ") {
        result = result.replace("  ", " ");
    }
    for p in ["!", "?", ",", ".", ";", ":", "\u{2026}", "\u{2014}"] {
        let pat = format!(" {}", p);
        while result.contains(&pat) {
            result = result.replace(&pat, p);
        }
    }
    result.trim().to_string()
}
'''


def patch_actions_rs():
    src = ACTIONS_RS.read_text(encoding="utf-8")
    if "apply_phrase_substitutions" in src:
        print("actions.rs already patched - skipping")
        return

    m = re.search(r"^pub\(crate\) async fn process_transcription_output", src, re.MULTILINE)
    if not m:
        raise RuntimeError("Couldn't find process_transcription_output in actions.rs")
    src = src[:m.start()] + APPLY_FN + "\n" + src[m.start():]

    m = re.search(r"^([ \t]*)if post_process \{", src, re.MULTILINE)
    if not m:
        raise RuntimeError("Couldn't find post_process branch in process_transcription_output")
    indent = m.group(1)
    inject_block = (
        f'{indent}// User-defined phrase substitutions.\n'
        f'{indent}if !settings.phrase_substitutions.is_empty() {{\n'
        f'{indent}    let before = final_text.clone();\n'
        f'{indent}    final_text = apply_phrase_substitutions(&final_text, &settings.phrase_substitutions);\n'
        f'{indent}    if final_text != before {{\n'
        f'{indent}        debug!("Phrase substitutions applied. Before: {{}} chars, After: {{}} chars", before.len(), final_text.len());\n'
        f'{indent}    }}\n'
        f'{indent}}}\n\n'
    )
    src = src[:m.start()] + inject_block + src[m.start():]

    ACTIONS_RS.write_text(src, encoding="utf-8")
    print("actions.rs patched")


def patch_tauri_conf():
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    changed = False

    name = conf.get("productName", "")
    if name.startswith("Handy") and name != "Handy Punct":
        conf["productName"] = "Handy Punct"
        if "identifier" in conf and conf["identifier"].startswith("com.pais.handy"):
            conf["identifier"] = "com.pais.handy.punct"
        if "version" in conf and not conf["version"].endswith("-punct"):
            conf["version"] = conf["version"] + "-punct"
        changed = True

    bundle = conf.get("bundle", {})
    if bundle.get("createUpdaterArtifacts"):
        bundle["createUpdaterArtifacts"] = False
        changed = True
    win = bundle.get("windows", {})
    if "signCommand" in win:
        del win["signCommand"]
        changed = True
    if "certificateThumbprint" in win:
        del win["certificateThumbprint"]
        changed = True

    plugins = conf.get("plugins", {})
    if "updater" in plugins:
        del plugins["updater"]
        changed = True

    if changed:
        TAURI_CONF.write_text(json.dumps(conf, indent=2, ensure_ascii=False), encoding="utf-8")
        print("tauri.conf.json patched (Handy Punct, signing/updater stripped)")
    else:
        print("tauri.conf.json: nothing to do")


def main():
    try:
        patch_settings_rs()
        patch_actions_rs()
        patch_tauri_conf()
        print("\nAll patches applied successfully.")
    except Exception as e:
        print(f"PATCH FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
