"use client";

import React, { useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { PageShell } from "@/components/ui/PageShell";
import { listMemories, searchMemories, deleteMemory } from "@/lib/api/memory";
import { Memory, MemorySearchResult } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";
import { Search, Trash2 } from "lucide-react";

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [searchResults, setSearchResults] = useState<MemorySearchResult[] | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [searching, setSearching] = useState(false);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);

  const fetchMemories = async () => {
    try {
      setLoading(true);
      const res = await listMemories();
      setMemories(res.memories || []);
      setSearchResults(null);
    } catch (err) {
      console.error("Failed to load memories", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMemories();
  }, []);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) {
      setSearchResults(null);
      return;
    }
    try {
      setSearching(true);
      const res = await searchMemories(searchQuery.trim(), 20);
      setSearchResults(res.results || []);
    } catch (err) {
      console.error("Search failed", err);
    } finally {
      setSearching(false);
    }
  };

  const handleDelete = async (memoryId: string) => {
    if (!confirm("Delete this memory permanently?")) return;
    try {
      await deleteMemory(memoryId);
      setMemories((prev) => prev.filter((m) => (m.id || (m as any).memory_id) !== memoryId));
      if (searchResults) {
        setSearchResults((prev) =>
          prev ? prev.filter((r) => r.memory_id !== memoryId) : null
        );
      }
      setSelectedMemory(null);
    } catch (err) {
      console.error("Failed to delete memory", err);
      alert("Failed to delete memory.");
    }
  };

  const displayItems = searchResults || memories;

  return (
    <PageShell
      title="Memory"
      action={
        <Button variant="outline" size="sm" onClick={fetchMemories}>
          Refresh
        </Button>
      }
    >
      <div className="space-y-6">
        <form onSubmit={handleSearch} className="flex items-center gap-2 max-w-md w-full">
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-neutral-400 absolute left-3 top-3" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search memories..."
              className="w-full bg-neutral-900 border border-neutral-800 rounded-lg pl-9 pr-4 py-2 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-neutral-700"
            />
          </div>
          <Button type="submit" size="sm" disabled={searching} isLoading={searching}>
            Search
          </Button>
          {searchResults && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setSearchQuery("");
                setSearchResults(null);
              }}
            >
              Clear
            </Button>
          )}
        </form>

        {loading ? (
          <div className="py-16 text-center">
            <p className="text-sm text-neutral-400">Loading memories...</p>
          </div>
        ) : displayItems.length === 0 ? (
          <div className="py-16 text-center">
            <p className="text-sm text-neutral-400">No memories found.</p>
            <p className="text-xs text-neutral-500 mt-1">
              {searchResults ? "Try a different search query." : "Nexus extracts memories automatically during chat."}
            </p>
          </div>
        ) : (
          <div className="divide-y divide-neutral-800 border-y border-neutral-800">
            {displayItems.map((item) => {
              const mem = searchResults ? ((item as any).memory || item) : item;
              const memId = mem.id || mem.memory_id || (item as any).memory_id;
              return (
                <div key={memId} className="py-4 flex items-start justify-between group hover:bg-neutral-900/50 -mx-4 px-4 transition-colors cursor-pointer" onClick={() => setSelectedMemory(mem)}>
                  <div className="flex-1 pr-6">
                    <p className="text-sm text-neutral-100 leading-relaxed line-clamp-2">{mem.content}</p>
                    <div className="flex items-center gap-3 mt-2 text-xs text-neutral-500">
                      <span>{formatDate(mem.created_at)}</span>
                      <span>Score: {mem.confidence ? mem.confidence.toFixed(2) : "1.00"}</span>
                      {searchResults && (
                        <span className="text-blue-500">
                          Match: {((item as any).similarity * 100).toFixed(1)}%
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <Badge variant="outline">{mem.category || "general"}</Badge>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(memId);
                      }}
                      className="p-1.5 rounded text-neutral-500 hover:text-rose-500 hover:bg-neutral-800 opacity-0 group-hover:opacity-100 transition-all"
                      title="Delete memory"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {selectedMemory && (
        <Modal
          isOpen={!!selectedMemory}
          onClose={() => setSelectedMemory(null)}
          title="Memory Details"
          subtitle={`ID: ${truncateId(selectedMemory.id || (selectedMemory as any).memory_id, 8)}`}
        >
          <div className="space-y-4">
            <div>
              <label className="text-xs text-neutral-500 block mb-1">Content</label>
              <div className="p-3 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-100 whitespace-pre-wrap">
                {selectedMemory.content}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="text-xs text-neutral-500 block">Category</span>
                <span className="text-neutral-100">{selectedMemory.category || "general"}</span>
              </div>
              <div>
                <span className="text-xs text-neutral-500 block">Source</span>
                <span className="text-neutral-100">{selectedMemory.source_type || "chat"}</span>
              </div>
              <div>
                <span className="text-xs text-neutral-500 block">Created</span>
                <span className="text-neutral-100">{formatDate(selectedMemory.created_at)}</span>
              </div>
              <div>
                <span className="text-xs text-neutral-500 block">Confidence</span>
                <span className="text-neutral-100">{selectedMemory.confidence?.toFixed(2) || "1.00"}</span>
              </div>
            </div>
            <div className="pt-4 flex justify-end gap-2 border-t border-neutral-800">
              <Button
                variant="danger"
                size="sm"
                onClick={() => handleDelete(selectedMemory.id || (selectedMemory as any).memory_id)}
              >
                Delete Memory
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </PageShell>
  );
}
