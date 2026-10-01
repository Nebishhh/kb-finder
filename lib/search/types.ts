// One row from a ranking SQL function, shared by every search mode.
export type SearchRow = {
  rank: number;
  source_article_id: string;
  title: string;
  url: string;
  category: string | null;
  score: number;
};
