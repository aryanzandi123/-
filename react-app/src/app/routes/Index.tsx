import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

export function Index(): JSX.Element {
  const [value, setValue] = useState("");
  const navigate = useNavigate();

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim().toUpperCase();
    if (!trimmed) return;
    navigate(`/visualize/${encodeURIComponent(trimmed)}?spa=1`);
  };

  return (
    <main style={{ padding: 32, fontFamily: "system-ui, sans-serif" }}>
      <h1>ProPaths</h1>
      <p>Enter a protein symbol to visualize.</p>
      <form onSubmit={onSubmit}>
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="ATXN3"
          aria-label="Protein symbol"
          style={{ padding: 8, fontSize: 14, marginRight: 8 }}
        />
        <button type="submit" style={{ padding: "8px 16px" }}>
          Visualize
        </button>
      </form>
    </main>
  );
}
