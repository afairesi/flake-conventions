@test "removing Python comments preserves hash characters inside strings" {
  cd "$BATS_TEST_TMPDIR"
  printf '%s\n' 'text = "# literal"  # ordinary comment' >example.py
  "$out/bin/uncomment" example.py
  [[ "$(<example.py)" == 'text = "# literal"' ]]
}
