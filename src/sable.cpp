// SABLE — a from-scratch UCI chess engine.
// Bitboards, magic sliders, PVS search, transposition table, tapered eval.
// No external dependencies. Written to be correct before being fast, then fast.
#include <bits/stdc++.h>
using namespace std;

typedef uint64_t U64;
typedef int32_t Score;

// ---------------------------------------------------------------- basics
enum { WHITE = 0, BLACK = 1 };
enum { PAWN = 0, KNIGHT, BISHOP, ROOK, QUEEN, KING, PT_NB = 6 };
static const int EMPTY = 12;
static inline int pcMake(int c, int t) { return c * 6 + t; }
static inline int pcType(int p) { return p % 6; }
static inline int pcColor(int p) { return p / 6; }
static inline U64 bit(int s) { return 1ULL << s; }
static inline int popcnt(U64 b) { return __builtin_popcountll(b); }
static inline int lsb(U64 b) { return __builtin_ctzll(b); }
static inline int msb(U64 b) { return 63 - __builtin_clzll(b); }
static inline int poplsb(U64 &b) { int s = lsb(b); b &= b - 1; return s; }
static inline int sqFile(int s) { return s & 7; }
static inline int sqRank(int s) { return s >> 3; }

// squares a1..h8 = 0..63
static const int SQ_NONE = -1;

// castling rights bits
enum { WK = 1, WQ = 2, BK = 4, BQ = 8 };

// scores
static const Score SCORE_INF = 32000;
static const Score SCORE_MATE = 31000;
static const Score SCORE_NONE = 32001;
static const Score SCORE_DRAW = 0;

// ---------------------------------------------------------------- move encoding
// bits 0-5 from, 6-11 to, 12-14 promo piece type (+1, 0 = none), 15-17 flag
enum { FLAG_NONE = 0, FLAG_CAP = 1, FLAG_EP = 2, FLAG_CASTLE = 3, FLAG_DOUBLE = 4 };
static inline uint32_t mvMake(int from, int to, int promo = 0, int flag = FLAG_NONE) {
    return (uint32_t)from | ((uint32_t)to << 6) | ((uint32_t)promo << 12) | ((uint32_t)flag << 15);
}
static inline int mvFrom(uint32_t m) { return m & 63; }
static inline int mvTo(uint32_t m) { return (m >> 6) & 63; }
static inline int mvPromo(uint32_t m) { return ((m >> 12) & 7) - 1; } // -1 if none
static inline int mvFlag(uint32_t m) { return (m >> 15) & 7; }
static const uint32_t MV_NONE = 0xFFFFFFFFu;

// ---------------------------------------------------------------- leaper + slider tables
static U64 knightAtt[64], kingAtt[64], pawnAtt[2][64];
static U64 betweenBB[64][64]; // squares strictly between (0 if not aligned)

struct Magic {
    U64 mask = 0, magic = 0;
    U64 *att = nullptr;
    int shift = 0;
};
static Magic rookMagics[64], bishopMagics[64];
static U64 rookAttackTable[102400];
static U64 bishopAttackTable[5248];

static U64 rayAttack(int sq, U64 occ, int df, int dr) {
    U64 a = 0;
    int f = sqFile(sq) + df, r = sqRank(sq) + dr;
    while (f >= 0 && f < 8 && r >= 0 && r < 8) {
        int s = r * 8 + f;
        a |= bit(s);
        if (occ & bit(s)) break;
        f += df; r += dr;
    }
    return a;
}
static U64 rookAttSlow(int sq, U64 occ) {
    return rayAttack(sq, occ, 1, 0) | rayAttack(sq, occ, -1, 0) | rayAttack(sq, occ, 0, 1) | rayAttack(sq, occ, 0, -1);
}
static U64 bishopAttSlow(int sq, U64 occ) {
    return rayAttack(sq, occ, 1, 1) | rayAttack(sq, occ, -1, 1) | rayAttack(sq, occ, 1, -1) | rayAttack(sq, occ, -1, -1);
}
static U64 rookMaskCalc(int sq) {
    U64 a = 0;
    int f = sqFile(sq), r = sqRank(sq);
    for (int d = 1; d < 8; d++) { if (f + d < 7) a |= bit(r * 8 + f + d); if (f - d > 0) a |= bit(r * 8 + f - d); }
    for (int d = 1; d < 8; d++) { if (r + d < 7) a |= bit((r + d) * 8 + f); if (r - d > 0) a |= bit((r - d) * 8 + f); }
    return a;
}
static U64 bishopMaskCalc(int sq) {
    U64 a = 0;
    int f = sqFile(sq), r = sqRank(sq);
    for (int d = 1; d < 8; d++) {
        if (f + d < 7 && r + d < 7) a |= bit((r + d) * 8 + f + d);
        if (f - d > 0 && r + d < 7) a |= bit((r + d) * 8 + f - d);
        if (f + d < 7 && r - d > 0) a |= bit((r - d) * 8 + f + d);
        if (f - d > 0 && r - d > 0) a |= bit((r - d) * 8 + f - d);
    }
    return a;
}
static U64 rngState = 0x2545F4914F6CDD1DULL;
static U64 rnd64() { // splitmix64
    U64 z = (rngState += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}
static U64 sparseRand() { return rnd64() & rnd64() & rnd64(); }

static void initMagic(Magic &m, int sq, bool rook, U64 *base) {
    m.mask = rook ? rookMaskCalc(sq) : bishopMaskCalc(sq);
    int bits = popcnt(m.mask);
    m.shift = 64 - bits;
    int size = 1 << bits;
    vector<U64> occs(size), atts(size);
    // carry-rippler subset enumeration
    U64 sub = 0;
    for (int i = 0; i < size; i++) {
        occs[i] = sub;
        atts[i] = rook ? rookAttSlow(sq, sub) : bishopAttSlow(sq, sub);
        sub = (sub - m.mask) & m.mask;
    }
    vector<int> used(size, -1);
    vector<U64> table(size);
    for (int tries = 0; tries < 100000000; tries++) {
        U64 cand = sparseRand();
        if (popcnt((m.mask * cand) >> 56) < 6) continue;
        fill(used.begin(), used.end(), -1);
        bool ok = true;
        for (int i = 0; i < size; i++) {
            int idx = (int)((occs[i] * cand) >> m.shift);
            if (used[idx] != -1 && table[idx] != atts[i]) { ok = false; break; }
            used[idx] = i; table[idx] = atts[i];
            if (used[idx] == i && false) {}
        }
        if (!ok) continue;
        m.magic = cand;
        m.att = base;
        for (int i = 0; i < size; i++) {
            int idx = (int)((occs[i] * cand) >> m.shift);
            base[idx] = atts[i];
        }
        return;
    }
    fprintf(stderr, "magic search failed for square %d\n", sq);
    exit(1);
}

static void initTables() {
    for (int s = 0; s < 64; s++) {
        int f = sqFile(s), r = sqRank(s);
        knightAtt[s] = 0; kingAtt[s] = 0;
        const int kdf[8] = { 1, 2, 2, 1, -1, -2, -2, -1 };
        const int kdr[8] = { 2, 1, -1, -2, -2, -1, 1, 2 };
        for (int i = 0; i < 8; i++) {
            int nf = f + kdf[i], nr = r + kdr[i];
            if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) knightAtt[s] |= bit(nr * 8 + nf);
            int gf = f + kdf[(i * 2) % 8], gr = r + kdr[(i * 2) % 8];
            (void)gf; (void)gr;
        }
        for (int df = -1; df <= 1; df++)
            for (int dr = -1; dr <= 1; dr++) {
                if (!df && !dr) continue;
                int nf = f + df, nr = r + dr;
                if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) kingAtt[s] |= bit(nr * 8 + nf);
            }
        for (int c = 0; c < 2; c++) {
            pawnAtt[c][s] = 0;
            int nr = r + (c == WHITE ? 1 : -1);
            if (nr >= 0 && nr < 8) {
                if (f > 0) pawnAtt[c][s] |= bit(nr * 8 + f - 1);
                if (f < 7) pawnAtt[c][s] |= bit(nr * 8 + f + 1);
            }
        }
    }
    // between table
    for (int a = 0; a < 64; a++)
        for (int b = 0; b < 64; b++) betweenBB[a][b] = 0;
    for (int a = 0; a < 64; a++) {
        for (int df = -1; df <= 1; df++)
            for (int dr = -1; dr <= 1; dr++) {
                if (!df && !dr) continue;
                U64 path = 0;
                int f = sqFile(a) + df, r = sqRank(a) + dr;
                while (f >= 0 && f < 8 && r >= 0 && r < 8) {
                    int s = r * 8 + f;
                    betweenBB[a][s] = path;
                    path |= bit(s);
                    f += df; r += dr;
                }
            }
    }
    U64 *rp = rookAttackTable, *bp = bishopAttackTable;
    for (int s = 0; s < 64; s++) {
        initMagic(rookMagics[s], s, true, rp);
        rp += 1 << popcnt(rookMaskCalc(s));
        initMagic(bishopMagics[s], s, false, bp);
        bp += 1 << popcnt(bishopMaskCalc(s));
    }
}
static inline U64 rookAtt(int sq, U64 occ) {
    const Magic &m = rookMagics[sq];
    return m.att[((occ & m.mask) * m.magic) >> m.shift];
}
static inline U64 bishopAtt(int sq, U64 occ) {
    const Magic &m = bishopMagics[sq];
    return m.att[((occ & m.mask) * m.magic) >> m.shift];
}

// ---------------------------------------------------------------- zobrist
static U64 Zp[2][6][64], Zcastle[16], Zside, Zep[8]; // ep stored by file for stability

// ---------------------------------------------------------------- position
struct Undo { U64 hash; int castle, ep, fifty; int cap; };
struct Pos {
    U64 bb[2][6];
    U64 occC[2];
    int board[64];
    int side, castle, ep, fifty;
    int ksq[2];
    U64 hash;
    int histPly;
    Undo stack[1024];
    // material counts for quick phase
    int phase;
};

static void posClear(Pos &p) {
    memset(p.bb, 0, sizeof(p.bb));
    p.occC[0] = p.occC[1] = 0;
    for (int i = 0; i < 64; i++) p.board[i] = EMPTY;
    p.side = WHITE; p.castle = 0; p.ep = -1; p.fifty = 0;
    p.ksq[0] = p.ksq[1] = SQ_NONE;
    p.hash = 0; p.histPly = 0; p.phase = 0;
}
static inline U64 occAll(const Pos &p) { return p.occC[0] | p.occC[1]; }

static void putPiece(Pos &p, int sq, int pc) {
    p.bb[pcColor(pc)][pcType(pc)] |= bit(sq);
    p.occC[pcColor(pc)] |= bit(sq);
    p.board[sq] = pc;
    p.hash ^= Zp[pcColor(pc)][pcType(pc)][sq];
    if (pcType(pc) == KING) p.ksq[pcColor(pc)] = sq;
}
static void delPiece(Pos &p, int sq) {
    int pc = p.board[sq];
    p.bb[pcColor(pc)][pcType(pc)] &= ~bit(sq);
    p.occC[pcColor(pc)] &= ~bit(sq);
    p.board[sq] = EMPTY;
    p.hash ^= Zp[pcColor(pc)][pcType(pc)][sq];
}
static void setCastle(Pos &p, int c) { p.hash ^= Zcastle[p.castle]; p.castle = c; p.hash ^= Zcastle[c]; }
static void setEp(Pos &p, int sq) {
    if (p.ep >= 0) p.hash ^= Zep[sqFile(p.ep)];
    p.ep = sq;
    if (sq >= 0) p.hash ^= Zep[sqFile(sq)];
}
static void setSide(Pos &p, int s) { p.side = s; p.hash ^= Zside; }

static void computePhase(Pos &p) {
    static const int pv[6] = { 0, 1, 1, 2, 4, 0 };
    int ph = 0;
    for (int c = 0; c < 2; c++)
        for (int t = KNIGHT; t <= QUEEN; t++) ph += pv[t] * popcnt(p.bb[c][t]);
    p.phase = min(ph, 24);
}

// FEN
static bool posFromFen(Pos &p, const string &fen) {
    posClear(p);
    istringstream ss(fen);
    string boardStr, sideStr, castleStr, epStr;
    ss >> boardStr >> sideStr >> castleStr >> epStr;
    if (boardStr.empty()) return false;
    int rank = 7, file = 0;
    for (char ch : boardStr) {
        if (ch == '/') { rank--; file = 0; continue; }
        if (isdigit((unsigned char)ch)) { file += ch - '0'; continue; }
        int c = isupper((unsigned char)ch) ? WHITE : BLACK;
        char l = tolower(ch);
        int t = l == 'p' ? PAWN : l == 'n' ? KNIGHT : l == 'b' ? BISHOP : l == 'r' ? ROOK : l == 'q' ? QUEEN : KING;
        if (rank < 0 || rank > 7 || file < 0 || file > 7) return false;
        putPiece(p, rank * 8 + file, pcMake(c, t));
        file++;
    }
    p.side = (sideStr == "b") ? BLACK : WHITE;
    p.castle = 0;
    for (char ch : castleStr) {
        if (ch == 'K') p.castle |= WK;
        else if (ch == 'Q') p.castle |= WQ;
        else if (ch == 'k') p.castle |= BK;
        else if (ch == 'q') p.castle |= BQ;
    }
    p.ep = -1;
    if (epStr.size() == 2 && epStr[0] >= 'a' && epStr[0] <= 'h')
        p.ep = (epStr[1] - '1') * 8 + (epStr[0] - 'a');
    int fifty = 0, full = 1;
    if (ss >> fifty) { p.fifty = fifty; if (ss >> full) {} }
    if (p.side == BLACK) p.hash ^= Zside;
    p.hash ^= Zcastle[p.castle];
    if (p.ep >= 0) p.hash ^= Zep[sqFile(p.ep)];
    computePhase(p);
    return true;
}
static string posToFen(const Pos &p) {
    string s;
    for (int r = 7; r >= 0; r--) {
        int empty = 0;
        for (int f = 0; f < 8; f++) {
            int pc = p.board[r * 8 + f];
            if (pc == EMPTY) { empty++; continue; }
            if (empty) { s += char('0' + empty); empty = 0; }
            const char *l = "pnbrqk";
            char ch = l[pcType(pc)];
            s += pcColor(pc) == WHITE ? toupper(ch) : ch;
        }
        if (empty) s += char('0' + empty);
        if (r) s += '/';
    }
    s += p.side == WHITE ? " w " : " b ";
    string c;
    if (p.castle & WK) c += 'K';
    if (p.castle & WQ) c += 'Q';
    if (p.castle & BK) c += 'k';
    if (p.castle & BQ) c += 'q';
    s += c.empty() ? "-" : c;
    s += ' ';
    if (p.ep >= 0) { s += char('a' + sqFile(p.ep)); s += char('1' + sqRank(p.ep)); }
    else s += '-';
    s += ' ' + to_string(p.fifty) + ' ' + to_string(1 + p.histPly / 2);
    return s;
}

// ---------------------------------------------------------------- attacks
static inline bool attacked(const Pos &p, int sq, int bySide) {
    if (pawnAtt[bySide ^ 1][sq] & p.bb[bySide][PAWN]) return true;
    if (knightAtt[sq] & p.bb[bySide][KNIGHT]) return true;
    if (kingAtt[sq] & p.bb[bySide][KING]) return true;
    U64 occ = occAll(p);
    if (bishopAtt(sq, occ) & (p.bb[bySide][BISHOP] | p.bb[bySide][QUEEN])) return true;
    if (rookAtt(sq, occ) & (p.bb[bySide][ROOK] | p.bb[bySide][QUEEN])) return true;
    return false;
}
static inline bool inCheck(const Pos &p, int side) { return attacked(p, p.ksq[side], side ^ 1); }

// ---------------------------------------------------------------- move generation
static inline void addMove(uint32_t *list, int &n, uint32_t m) { list[n++] = m; }

static int genMoves(const Pos &p, uint32_t *list, bool capturesOnly = false) {
    int n = 0;
    int us = p.side, them = us ^ 1;
    U64 occ = occAll(p);
    U64 usBB = p.occC[us], themBB = p.occC[them];
    U64 empty = ~occ;

    // pawns
    U64 pawns = p.bb[us][PAWN];
    int up = us == WHITE ? 8 : -8;
    int startRank = us == WHITE ? 1 : 6;
    int promoRank = us == WHITE ? 6 : 1;
    while (pawns) {
        int s = poplsb(pawns);
        int to = s + up;
        bool promo = sqRank(s) == promoRank;
        if (!capturesOnly) {
            if (to >= 0 && to < 64 && p.board[to] == EMPTY) {
                if (promo) {
                    for (int t = QUEEN; t >= KNIGHT; t--) addMove(list, n, mvMake(s, to, t + 1, FLAG_NONE));
                } else {
                    addMove(list, n, mvMake(s, to));
                    if (sqRank(s) == startRank && p.board[to + up] == EMPTY)
                        addMove(list, n, mvMake(s, to + up, 0, FLAG_DOUBLE));
                }
            }
        }
        U64 caps = pawnAtt[us][s] & themBB;
        while (caps) {
            int t = poplsb(caps);
            if (promo) { for (int pt = QUEEN; pt >= KNIGHT; pt--) addMove(list, n, mvMake(s, t, pt + 1, FLAG_CAP)); }
            else addMove(list, n, mvMake(s, t, 0, FLAG_CAP));
        }
        if (p.ep >= 0 && (pawnAtt[us][s] & bit(p.ep))) addMove(list, n, mvMake(s, p.ep, 0, FLAG_EP));
    }

    // knights
    U64 b = p.bb[us][KNIGHT];
    while (b) {
        int s = poplsb(b);
        U64 t = knightAtt[s] & ~usBB;
        if (capturesOnly) t &= themBB;
        while (t) { int d = poplsb(t); addMove(list, n, mvMake(s, d, 0, (themBB & bit(d)) ? FLAG_CAP : FLAG_NONE)); }
    }
    // bishops
    b = p.bb[us][BISHOP];
    while (b) {
        int s = poplsb(b);
        U64 t = bishopAtt(s, occ) & ~usBB;
        if (capturesOnly) t &= themBB;
        while (t) { int d = poplsb(t); addMove(list, n, mvMake(s, d, 0, (themBB & bit(d)) ? FLAG_CAP : FLAG_NONE)); }
    }
    // rooks
    b = p.bb[us][ROOK];
    while (b) {
        int s = poplsb(b);
        U64 t = rookAtt(s, occ) & ~usBB;
        if (capturesOnly) t &= themBB;
        while (t) { int d = poplsb(t); addMove(list, n, mvMake(s, d, 0, (themBB & bit(d)) ? FLAG_CAP : FLAG_NONE)); }
    }
    // queens
    b = p.bb[us][QUEEN];
    while (b) {
        int s = poplsb(b);
        U64 t = (bishopAtt(s, occ) | rookAtt(s, occ)) & ~usBB;
        if (capturesOnly) t &= themBB;
        while (t) { int d = poplsb(t); addMove(list, n, mvMake(s, d, 0, (themBB & bit(d)) ? FLAG_CAP : FLAG_NONE)); }
    }
    // king
    {
        int s = p.ksq[us];
        U64 t = kingAtt[s] & ~usBB;
        if (capturesOnly) t &= themBB;
        while (t) { int d = poplsb(t); addMove(list, n, mvMake(s, d, 0, (themBB & bit(d)) ? FLAG_CAP : FLAG_NONE)); }
    }
    // castling
    if (!capturesOnly) {
        if (us == WHITE) {
            if ((p.castle & WK) && p.board[5] == EMPTY && p.board[6] == EMPTY && p.board[4] == pcMake(WHITE, KING) &&
                p.board[7] == pcMake(WHITE, ROOK) && !attacked(p, 4, BLACK) && !attacked(p, 5, BLACK) && !attacked(p, 6, BLACK))
                addMove(list, n, mvMake(4, 6, 0, FLAG_CASTLE));
            if ((p.castle & WQ) && p.board[3] == EMPTY && p.board[2] == EMPTY && p.board[1] == EMPTY && p.board[4] == pcMake(WHITE, KING) &&
                p.board[0] == pcMake(WHITE, ROOK) && !attacked(p, 4, BLACK) && !attacked(p, 3, BLACK) && !attacked(p, 2, BLACK))
                addMove(list, n, mvMake(4, 2, 0, FLAG_CASTLE));
        } else {
            if ((p.castle & BK) && p.board[61] == EMPTY && p.board[62] == EMPTY && p.board[60] == pcMake(BLACK, KING) &&
                p.board[63] == pcMake(BLACK, ROOK) && !attacked(p, 60, WHITE) && !attacked(p, 61, WHITE) && !attacked(p, 62, WHITE))
                addMove(list, n, mvMake(60, 62, 0, FLAG_CASTLE));
            if ((p.castle & BQ) && p.board[59] == EMPTY && p.board[58] == EMPTY && p.board[57] == EMPTY && p.board[60] == pcMake(BLACK, KING) &&
                p.board[56] == pcMake(BLACK, ROOK) && !attacked(p, 60, WHITE) && !attacked(p, 59, WHITE) && !attacked(p, 58, WHITE))
                addMove(list, n, mvMake(60, 58, 0, FLAG_CASTLE));
        }
    }
    return n;
}

// ---------------------------------------------------------------- make / unmake
static inline int castleRightsMask(int sq) {
    switch (sq) {
        case 0: return WQ; case 4: return WK | WQ; case 7: return WK;
        case 56: return BQ; case 60: return BK | BQ; case 63: return BK;
        default: return 0;
    }
}

static void unmakeNoCheck(Pos &p, uint32_t m);
static string moveToUci(uint32_t m);
static void makeNull(Pos &p);
static void unmakeNull(Pos &p);

// returns false if the move leaves our own king in check (illegal), after undoing it
static bool make(Pos &p, uint32_t m) {
    int from = mvFrom(m), to = mvTo(m), promo = mvPromo(m), flag = mvFlag(m);
    int us = p.side, them = us ^ 1;
    Undo &u = p.stack[p.histPly];
    u.hash = p.hash; u.castle = p.castle; u.ep = p.ep; u.fifty = p.fifty; u.cap = EMPTY;

    int pc = p.board[from];
    int pt = pcType(pc);
    int captured = EMPTY;
    int capSq = to;

    if (flag == FLAG_EP) {
        capSq = to + (us == WHITE ? -8 : 8);
        captured = p.board[capSq];
    } else if (p.board[to] != EMPTY) {
        captured = p.board[to];
    }
    u.cap = captured;

    // reset ep and castle hash before changing
    if (p.ep >= 0) p.hash ^= Zep[sqFile(p.ep)];
    p.ep = -1;
    p.hash ^= Zcastle[p.castle];

    if (captured != EMPTY) {
        delPiece(p, capSq);
        p.fifty = 0;
    } else if (pt == PAWN) p.fifty = 0;
    else p.fifty++;

    delPiece(p, from);

    if (promo >= 0) putPiece(p, to, pcMake(us, promo));
    else putPiece(p, to, pc);

    if (flag == FLAG_DOUBLE) {
        p.ep = (from + to) / 2;
    }
    if (flag == FLAG_CASTLE) {
        if (to == 6) { delPiece(p, 7); putPiece(p, 5, pcMake(WHITE, ROOK)); }
        else if (to == 2) { delPiece(p, 0); putPiece(p, 3, pcMake(WHITE, ROOK)); }
        else if (to == 62) { delPiece(p, 63); putPiece(p, 61, pcMake(BLACK, ROOK)); }
        else if (to == 58) { delPiece(p, 56); putPiece(p, 59, pcMake(BLACK, ROOK)); }
    }

    int c = p.castle & ~(castleRightsMask(from) | castleRightsMask(capSq));
    if (c != p.castle) { p.castle = c; }
    p.hash ^= Zcastle[p.castle];
    if (p.ep >= 0) p.hash ^= Zep[sqFile(p.ep)];

    p.hash ^= Zside;
    p.side = them;
    p.histPly++;

    if (attacked(p, p.ksq[us], them)) { unmakeNoCheck(p, m); return false; }
    return true;
}

static void unmakeNoCheck(Pos &p, uint32_t m) {
    int from = mvFrom(m), to = mvTo(m), promo = mvPromo(m), flag = mvFlag(m);
    p.histPly--;
    int us = p.side ^ 1, them = p.side;
    Undo &u = p.stack[p.histPly];

    p.side = us;
    p.hash ^= Zside;

    if (flag == FLAG_CASTLE) {
        if (to == 6) { delPiece(p, 5); putPiece(p, 7, pcMake(WHITE, ROOK)); }
        else if (to == 2) { delPiece(p, 3); putPiece(p, 0, pcMake(WHITE, ROOK)); }
        else if (to == 62) { delPiece(p, 61); putPiece(p, 63, pcMake(BLACK, ROOK)); }
        else if (to == 58) { delPiece(p, 59); putPiece(p, 56, pcMake(BLACK, ROOK)); }
    }

    int movedPiece = p.board[to];
    delPiece(p, to);
    if (promo >= 0) putPiece(p, from, pcMake(us, PAWN));
    else putPiece(p, from, movedPiece);

    if (u.cap != EMPTY) {
        if (flag == FLAG_EP) putPiece(p, to + (us == WHITE ? -8 : 8), u.cap);
        else putPiece(p, to, u.cap);
    }
    (void)them;
    p.castle = u.castle;
    p.ep = u.ep;
    p.fifty = u.fifty;
    p.hash = u.hash;
}
static void unmake(Pos &p, uint32_t m) { unmakeNoCheck(p, m); }

static void makeNull(Pos &p) {
    Undo &u = p.stack[p.histPly];
    u.hash = p.hash; u.castle = p.castle; u.ep = p.ep; u.fifty = p.fifty; u.cap = EMPTY;
    if (p.ep >= 0) p.hash ^= Zep[sqFile(p.ep)];
    p.ep = -1;
    p.hash ^= Zside;
    p.side ^= 1;
    p.fifty++;
    p.histPly++;
}
static void unmakeNull(Pos &p) {
    p.histPly--;
    p.side ^= 1;
    p.hash = p.stack[p.histPly].hash;
    p.ep = p.stack[p.histPly].ep;
    p.castle = p.stack[p.histPly].castle;
    p.fifty = p.stack[p.histPly].fifty;
}

// repetition and fifty-move awareness: an engine that cannot see a draw
// will happily repeat a winning position or defend a drawn one badly
static bool isRepetition(const Pos &p) {
    int end = p.histPly - p.fifty;
    if (end < 0) end = 0;
    for (int i = p.histPly - 2; i >= end; i -= 2)
        if (p.stack[i].hash == p.hash) return true;
    return false;
}
static bool insufficientMaterial(const Pos &p) {
    if (p.occC[0] & (p.bb[0][PAWN] | p.bb[0][ROOK] | p.bb[0][QUEEN])) return false;
    if (p.occC[1] & (p.bb[1][PAWN] | p.bb[1][ROOK] | p.bb[1][QUEEN])) return false;
    int n0 = popcnt(p.bb[0][KNIGHT]), b0 = popcnt(p.bb[0][BISHOP]);
    int n1 = popcnt(p.bb[1][KNIGHT]), b1 = popcnt(p.bb[1][BISHOP]);
    int minor = n0 + b0 + n1 + b1;
    if (minor <= 1) return true;
    if (n0 + n1 == 0 && (b0 <= 1 && b1 <= 1) && minor <= 2 && (b0 == b1)) return true;
    return false;
}

// perft
static U64 perft(Pos &p, int depth) {
    if (depth == 0) return 1;
    uint32_t list[256];
    int n = genMoves(p, list);
    U64 nodes = 0;
    for (int i = 0; i < n; i++) {
        if (!make(p, list[i])) continue;
        if (depth == 1) nodes++;
        else nodes += perft(p, depth - 1);
        unmake(p, list[i]);
    }
    return nodes;
}

// ---------------------------------------------------------------- evaluation
static const int MAT_MG[6] = { 100, 320, 330, 500, 900, 0 };
static const int MAT_EG[6] = { 130, 300, 320, 520, 950, 0 };

static int PSQT_MG[6][64], PSQT_EG[6][64];

static void initPSQT() {
    // programmatic, tapered tables. values are bonuses over material.
    for (int s = 0; s < 64; s++) {
        int f = sqFile(s), r = sqRank(s);
        int cf = min(f, 7 - f);           // 0..3 distance from edge
        int centerDist = abs(2 * f - 7) + abs(2 * r - 7); // 0 centre .. 12 corner-ish
        // pawns: advance, central files
        int rel = r; // white perspective; mirrored below
        PSQT_MG[PAWN][s] = 0;
        PSQT_EG[PAWN][s] = 0;
        (void)cf;
        (void)rel;
    }
    for (int t = 0; t < 6; t++)
        for (int s = 0; s < 64; s++) {
            int f = sqFile(s), r = sqRank(s);
            int cf = min(f, 7 - f);
            int cd = abs(2 * f - 7) + abs(2 * r - 7);
            int mg = 0, eg = 0;
            if (t == PAWN) {
                mg = r * 3 - 10 + cf * 4;
                eg = r * 6 - 10 + cf * 3;
            } else if (t == KNIGHT) {
                mg = (5 - cd) * 8 - 6;
                eg = (5 - cd) * 5 - 4;
            } else if (t == BISHOP) {
                mg = (5 - cd) * 4;
                eg = (5 - cd) * 4;
            } else if (t == ROOK) {
                mg = cf * 2;
                eg = cf * 2 + (r == 6 ? 15 : 0);
            } else if (t == QUEEN) {
                mg = (5 - cd) * 1;
                eg = (5 - cd) * 1;
            } else { // king
                if (r <= 1) { mg = (r == 0 ? 28 : 12) + cf * 4; eg = -r * 14 - cf * 6; }
                else { mg = -r * 6 + cf * 2; eg = -r * 12 - cf * 8; }
            }
            PSQT_MG[t][s] = mg;
            PSQT_EG[t][s] = eg;
        }
    // mirror for black
    for (int t = 0; t < 6; t++)
        for (int s = 0; s < 64; s++) { }
}

static inline int mirrorSq(int s) { return (7 - sqRank(s)) * 8 + sqFile(s); }

static int evalPawnShield(const Pos &p, int color) {
    int k = p.ksq[color];
    int f = sqFile(k), r = sqRank(k);
    int dir = color == WHITE ? 1 : -1;
    int score = 0;
    for (int df = -1; df <= 1; df++) {
        int ff = f + df;
        if (ff < 0 || ff > 7) continue;
        int rr = r + dir;
        bool found = false;
        for (int dist = 1; dist <= 2; dist++) {
            int r2 = r + dir * dist;
            if (r2 < 0 || r2 > 7) continue;
            if (p.board[r2 * 8 + ff] == pcMake(color, PAWN)) {
                score += dist == 1 ? 12 : 6;
                found = true;
                break;
            }
        }
        if (!found) score -= 12;
    }
    return score;
}

static int evalKingAttack(const Pos &p, int color) {
    // enemy pieces eyeing squares around our king
    int k = p.ksq[color ^ 1]; // our king as seen by them? no: we evaluate attacks ON color's king
    k = p.ksq[color];
    U64 zone = kingAtt[k] | bit(k);
    int them = color ^ 1;
    int score = 0;
    U64 occ = occAll(p);
    U64 att = 0;
    U64 b = p.bb[them][KNIGHT];
    while (b) { int s = poplsb(b); att |= knightAtt[s]; }
    b = p.bb[them][BISHOP];
    while (b) { int s = poplsb(b); att |= bishopAtt(s, occ); }
    b = p.bb[them][ROOK];
    while (b) { int s = poplsb(b); att |= rookAtt(s, occ); }
    b = p.bb[them][QUEEN];
    while (b) { int s = poplsb(b); att |= bishopAtt(s, occ) | rookAtt(s, occ); }
    if (p.occC[them] & p.bb[them][PAWN]) {
        b = p.bb[them][PAWN];
        while (b) { int s = poplsb(b); att |= pawnAtt[them][s]; }
    }
    score += popcnt(att & zone) * 9;
    return score;
}

static int evaluate(const Pos &p) {
    int mg = 0, eg = 0;
    U64 occ = occAll(p);
    int bishops[2] = { 0, 0 };
    int pawnFileCount[2][8] = { { 0 } };
    for (int c = 0; c < 2; c++) {
        int sgn = c == WHITE ? 1 : -1;
        for (int t = PAWN; t <= KING; t++) {
            U64 b = p.bb[c][t];
            while (b) {
                int s = poplsb(b);
                int ps = c == WHITE ? s : mirrorSq(s);
                mg += sgn * (MAT_MG[t] + PSQT_MG[t][ps]);
                eg += sgn * (MAT_EG[t] + PSQT_EG[t][ps]);
                if (t == PAWN) pawnFileCount[c][sqFile(s)]++;
            }
            if (t == BISHOP) bishops[c] = popcnt(p.bb[c][BISHOP]);
        }
        // bishop pair
        if (bishops[c] >= 2) { mg += sgn * 28; eg += sgn * 48; }
        // pawn structure
        int dbld = 0, iso = 0, passed = 0;
        for (int f = 0; f < 8; f++) {
            if (pawnFileCount[c][f] > 1) dbld += pawnFileCount[c][f] - 1;
            if (pawnFileCount[c][f] > 0) {
                bool isoF = (f == 0 || pawnFileCount[c][f - 1] == 0) && (f == 7 || pawnFileCount[c][f + 1] == 0);
                if (isoF) iso += pawnFileCount[c][f];
            }
        }
        // passed pawns
        U64 b = p.bb[c][PAWN];
        while (b) {
            int s = poplsb(b);
            int f = sqFile(s), r = sqRank(s);
            U64 ahead = 0;
            for (int df = -1; df <= 1; df++) {
                int ff = f + df;
                if (ff < 0 || ff > 7) continue;
                for (int rr = 0; rr < 8; rr++) {
                    bool inFront = c == WHITE ? rr > r : rr < r;
                    if (!inFront) continue;
                    if (p.board[rr * 8 + ff] == pcMake(c ^ 1, PAWN)) ahead |= bit(rr * 8 + ff);
                }
            }
            if (!ahead) {
                int relRank = c == WHITE ? r : 7 - r;
                passed += relRank * relRank / 2;
            }
        }
        mg += sgn * (-12 * dbld - 16 * iso + 8 * passed);
        eg += sgn * (-18 * dbld - 20 * iso + 14 * passed);
        // rook files
        b = p.bb[c][ROOK];
        while (b) {
            int s = poplsb(b);
            int f = sqFile(s);
            bool ownPawnOnFile = (p.bb[c][PAWN] & (0x0101010101010101ULL << f)) != 0;
            bool oppPawnOnFile = (p.bb[c ^ 1][PAWN] & (0x0101010101010101ULL << f)) != 0;
            if (!ownPawnOnFile && !oppPawnOnFile) { mg += sgn * 22; eg += sgn * 12; }
            else if (!ownPawnOnFile) { mg += sgn * 10; eg += sgn * 6; }
        }
        // king safety (midgame only)
        mg += sgn * evalPawnShield(p, c);
        mg += sgn * evalKingAttack(p, c);
        // mobility (cheap)
        {
            U64 mob = 0;
            b = p.bb[c][KNIGHT]; while (b) { int s = poplsb(b); mob += popcnt(knightAtt[s] & ~p.occC[c]); }
            b = p.bb[c][BISHOP]; while (b) { int s = poplsb(b); mob += popcnt(bishopAtt(s, occ) & ~p.occC[c]); }
            b = p.bb[c][ROOK]; while (b) { int s = poplsb(b); mob += popcnt(rookAtt(s, occ) & ~p.occC[c]); }
            b = p.bb[c][QUEEN]; while (b) { int s = poplsb(b); mob += popcnt((bishopAtt(s, occ) | rookAtt(s, occ)) & ~p.occC[c]); }
            mg += sgn * (mob / 2);
        }
    }
    int phase = p.phase;
    int score = (mg * phase + eg * (24 - phase)) / 24;
    score += 10; // tempo for side to move
    return p.side == WHITE ? score : -score;
}

// ---------------------------------------------------------------- transposition table
struct TTEntry {
    U64 key = 0;
    uint32_t move = MV_NONE;
    int16_t score = 0;
    int8_t depth = -1;
    uint8_t flag = 0; // 0 empty, 1 exact, 2 lower, 3 upper
    uint8_t age = 0;
};
static vector<TTEntry> TT;
static size_t TT_MASK = 0;
static uint8_t TT_AGE = 0;

static void ttInit(size_t mb) {
    size_t entries = (mb * 1024 * 1024) / sizeof(TTEntry);
    size_t n = 1;
    while (n * 2 <= entries) n *= 2;
    TT.assign(n, TTEntry());
    TT_MASK = n - 1;
}
static void ttClear() { fill(TT.begin(), TT.end(), TTEntry()); TT_AGE = 0; }

static TTEntry *ttProbe(U64 key) {
    TTEntry *e = &TT[key & TT_MASK];
    if (e->flag != 0 && e->key == key) return e;
    return nullptr;
}
static void ttStore(U64 key, int depth, Score score, int flag, uint32_t move) {
    TTEntry *e = &TT[key & TT_MASK];
    bool same = (e->flag != 0 && e->key == key && e->age == TT_AGE);
    if (same && e->depth > depth + 2) {
        if (move != MV_NONE) e->move = move;
        return;
    }
    e->key = key;
    e->depth = (int8_t)depth;
    e->flag = (uint8_t)flag;
    e->age = TT_AGE;
    if (move != MV_NONE || !same) e->move = move;
    e->score = (int16_t)max(-30000, min(30000, (int)score));
}

// ---------------------------------------------------------------- opening book
// Learned from the engine's own wins. Deliberately simple: a plain text map from
// position (board+side+castling+ep) to a move that has scored well for us. It
// buys clock time in fast games, which matters more than it sounds in 1+0.
static unordered_map<string, string> bookMoves;
static unordered_set<string> avoidMoves;   // "key|uci" pairs learned from mistakes

static void loadAvoid(const string &path) {
    ifstream f(path);
    if (!f) return;
    string line;
    while (getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if (line.find('|') != string::npos) avoidMoves.insert(line);
    }
    if (!avoidMoves.empty())
        fprintf(stderr, "info string avoid-list loaded: %zu entries\n", avoidMoves.size());
}

static string bookKey(const Pos &p) {
    // board + side to move only: transposition-friendly and stable across games
    string k;
    k.reserve(70);
    for (int r = 7; r >= 0; r--) {
        int empty = 0;
        for (int f = 0; f < 8; f++) {
            int pc = p.board[r * 8 + f];
            if (pc == EMPTY) { empty++; continue; }
            if (empty) { k += char('0' + empty); empty = 0; }
            const char *l = "pnbrqk";
            char ch = l[pcType(pc)];
            k += (pcColor(pc) == WHITE) ? toupper(ch) : ch;
        }
        if (empty) k += char('0' + empty);
        if (r) k += '/';
    }
    k += (p.side == WHITE) ? " w" : " b";
    return k;
}

// Where do book.txt / avoid.txt live? They used to be opened relative to the
// CURRENT DIRECTORY, so `./sable` from the repo root loaded the book and the same
// binary run from anywhere else silently played bookless -- different openings,
// different results, same command line. Resolve relative paths against the
// executable's own directory instead, and let an env var override.
static string exeDir() {
#ifdef __linux__
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = 0;
        string p(buf);
        size_t slash = p.find_last_of('/');
        if (slash != string::npos) return p.substr(0, slash);
    }
#endif
    return string(".");
}

static bool fileExists(const string &p) { ifstream f(p); return (bool)f; }

static string resolveDataFile(const string &path, const char *envVar, const string &fallbackName) {
    if (const char *e = getenv(envVar)) {
        if (*e) return string(e);
    }
    if (path.empty()) return exeDir() + "/" + fallbackName;
    if (path[0] == '/') return path;
    // Relative: prefer the one next to the binary, then the source-ish layout the
    // harness uses (tools/../book.txt is the repo root), then the cwd.
    vector<string> cands = {
        exeDir() + "/" + path,
        exeDir() + "/../" + path,
        exeDir() + "/../../" + path,
        path,
    };
    for (auto &c : cands) if (fileExists(c)) return c;
    return exeDir() + "/" + path;
}

static bool bookEnabled = true;

static void loadBook(const string &path) {
    ifstream f(path);
    if (!f) return;
    string line;
    while (getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        size_t bar = line.find('|');
        if (bar == string::npos) continue;
        string k = line.substr(0, bar), mv = line.substr(bar + 1);
        while (!mv.empty() && (mv.back() == '\r' || mv.back() == ' ')) mv.pop_back();
        if (!k.empty() && mv.size() >= 4) bookMoves[k] = mv;
    }
    if (!bookMoves.empty())
        fprintf(stderr, "info string book loaded: %zu entries\n", bookMoves.size());
}

// ---------------------------------------------------------------- search
struct SearchLimits {
    int depth = 128;
    int64_t movetime = -1;
    int64_t wtime = -1, btime = -1, winc = 0, binc = 0;
    int64_t nodes = -1;
    bool infinite = false;
    int movestogo = 0;
};
static chrono::steady_clock::time_point startTime;
static int64_t hardDeadline = -1; // ms
static bool timeUp = false;
static int64_t nodeLimit = -1;
static U64 nodesSearched = 0;

struct Killer { uint32_t m[2] = { MV_NONE, MV_NONE }; };
static Killer killers[128];
static int historyTab[2][64][64];

static inline bool isCapture(const Pos &p, uint32_t m) {
    return mvFlag(m) == FLAG_CAP || mvFlag(m) == FLAG_EP || p.board[mvTo(m)] != EMPTY;
}
static inline bool timeCheck() {
    if ((nodesSearched & 2047) != 0) return timeUp;
    if (nodeLimit > 0 && (int64_t)nodesSearched >= nodeLimit) { timeUp = true; return true; }
    if (hardDeadline > 0) {
        auto now = chrono::steady_clock::now();
        int64_t ms = chrono::duration_cast<chrono::milliseconds>(now - startTime).count();
        if (ms >= hardDeadline) { timeUp = true; return true; }
    }
    return timeUp;
}

// A conservative, obviously-correct replacement for full SEE:
// a capture is "bad" if a cheaper piece takes a costlier victim on a defended square.
static bool badCapture(const Pos &p, uint32_t m) {
    if (mvPromo(m) >= 0) return false;
    if (mvFlag(m) == FLAG_EP) return false;
    int to = mvTo(m);
    if (p.board[to] == EMPTY) return false;
    int victim = MAT_MG[pcType(p.board[to])];
    int attacker = MAT_MG[pcType(p.board[mvFrom(m)])];
    if (victim >= attacker) return false;
    return attacked(p, to, p.side ^ 1);
}

struct MoveList {
    uint32_t m[256];
    int score[256];
    int n = 0;
    void add(uint32_t mv, int sc) { m[n] = mv; score[n] = sc; n++; }
    void sortDesc(int lo = 0) {
        // insertion sort on score, descending
        for (int i = lo + 1; i < n; i++) {
            uint32_t km = m[i];
            int ks = score[i];
            int j = i - 1;
            while (j >= lo && score[j] < ks) { m[j + 1] = m[j]; score[j + 1] = score[j]; j--; }
            m[j + 1] = km; score[j + 1] = ks;
        }
    }
};

static int qsearch(Pos &p, int alpha, int beta, int ply);
static int search(Pos &p, int depth, int alpha, int beta, int ply, bool doNull);

static int qsearch(Pos &p, int alpha, int beta, int ply) {
    nodesSearched++;
    if (timeCheck()) return 0;
    bool inChk = inCheck(p, p.side);
    int stand = inChk ? -SCORE_INF : evaluate(p);
    if (!inChk) {
        if (ply > 100) return stand;
        if (stand >= beta) return stand;
        if (stand > alpha) alpha = stand;
    }
    MoveList ml;
    uint32_t raw[256];
    int n = genMoves(p, raw, !inChk);
    for (int i = 0; i < n; i++) {
        uint32_t m = raw[i];
        int from = mvFrom(m), to = mvTo(m);
        int t = (mvFlag(m) == FLAG_EP || p.board[to] == EMPTY) ? PAWN : pcType(p.board[to]);
        int sc = 10 * MAT_MG[t] - MAT_MG[pcType(p.board[from])];
        if (mvPromo(m) >= 0) sc += 500 * MAT_MG[mvPromo(m)];
        ml.add(m, sc);
    }
    ml.sortDesc();

    int best = inChk ? -SCORE_INF : stand;
    int legal = 0;
    for (int i = 0; i < ml.n; i++) {
        uint32_t m = ml.m[i];
        if (!inChk) {
            int tv = p.board[mvTo(m)] != EMPTY ? MAT_MG[pcType(p.board[mvTo(m)])] : MAT_MG[PAWN];
            if (stand + tv + 200 < alpha) continue;   // delta pruning
            if (badCapture(p, m)) continue;
        }
        if (!make(p, m)) continue;
        legal++;
        int sc = -qsearch(p, -beta, -alpha, ply + 1);
        unmake(p, m);
        if (timeUp) return best > -SCORE_INF ? best : 0;
        if (sc > best) {
            best = sc;
            if (sc > alpha) alpha = sc;
            if (sc >= beta) return sc;
        }
    }
    if (inChk && legal == 0) return -SCORE_MATE + ply;   // checkmate found by the eye, not the eval
    return best;
}

static int search(Pos &p, int depth, int alpha, int beta, int ply, bool doNull) {
    if (depth <= 0) return qsearch(p, alpha, beta, ply);
    nodesSearched++;
    if (timeCheck()) return 0;

    if (ply > 0) {
        if (p.fifty >= 100) return SCORE_DRAW;
        if (isRepetition(p)) return SCORE_DRAW;
        if (insufficientMaterial(p)) return SCORE_DRAW;
    }
    bool isPv = (beta - alpha) > 1;
    bool inChk = inCheck(p, p.side);

    if (inChk) depth++; // check extension
    if (ply >= 100) return evaluate(p);

    // mate distance pruning
    alpha = max(alpha, -SCORE_MATE + ply);
    beta = min(beta, SCORE_MATE - ply - 1);
    if (alpha >= beta) return alpha;

    // TT probe
    TTEntry *te = ttProbe(p.hash);
    uint32_t ttMove = MV_NONE;
    if (te) {
        ttMove = te->move;
        if (!isPv && te->depth >= depth) {
            Score s = te->score;
            if (s > SCORE_MATE - 1000) s -= ply;
            else if (s < -SCORE_MATE + 1000) s += ply;
            if (te->flag == 1) return s;
            if (te->flag == 2 && s >= beta) return s;
            if (te->flag == 3 && s <= alpha) return s;
        }
    }

    int staticEval = inChk ? SCORE_NONE : evaluate(p);
    if (!isPv && !inChk) {
        // reverse futility pruning
        if (depth <= 6 && staticEval - 85 * depth >= beta) return staticEval;
        // null move pruning
        if (doNull && depth >= 3 && staticEval >= beta && p.phase > 4) {
            int R = 2 + depth / 4;
            makeNull(p);
            int sc = -search(p, depth - 1 - R, -beta, -beta + 1, ply + 1, false);
            unmakeNull(p);
            if (timeUp) return 0;
            if (sc >= beta) {
                if (sc > SCORE_MATE - 1000) sc = beta;
                return sc;
            }
        }
        // razor / futility at shallow depth handled implicitly above
    }

    MoveList ml;
    uint32_t raw[256];
    int n = genMoves(p, raw);
    for (int i = 0; i < n; i++) {
        uint32_t m = raw[i];
        int sc = 0;
        if (m == ttMove) sc = 2000000;
        else if (isCapture(p, m)) {
            int from = mvFrom(m), to = mvTo(m);
            int victim = (mvFlag(m) == FLAG_EP) ? PAWN : (p.board[to] == EMPTY ? PAWN : pcType(p.board[to]));
            int attacker = pcType(p.board[from]);
            sc = 1000000 + 10 * MAT_MG[victim] - MAT_MG[attacker];
            if (mvPromo(m) >= 0) sc += 500 * MAT_MG[mvPromo(m)];
        } else if (m == killers[ply].m[0]) sc = 900000;
        else if (m == killers[ply].m[1]) sc = 800000;
        else {
            int f = mvFrom(m), t = mvTo(m);
            sc = historyTab[p.side][f][t];
        }
        ml.add(m, sc);
    }
    ml.sortDesc();

    int bestScore = -SCORE_INF;
    uint32_t bestMove = MV_NONE;
    int legal = 0;
    int origAlpha = alpha;

    for (int i = 0; i < ml.n; i++) {
        uint32_t m = ml.m[i];
        bool cap = isCapture(p, m);

        // futility pruning (shallow, not pv, not in check, not tactical)
        if (!isPv && !inChk && depth <= 4 && legal > 0 && !cap && mvPromo(m) < 0) {
            if (staticEval + 130 * depth <= alpha) continue;
        }
        // late move reduction conditions
        int reduction = 0;
        if (depth >= 3 && legal >= 3 && !cap && mvPromo(m) < 0 && !inChk) {
            reduction = 1;
            if (legal >= 6) reduction++;
            if (depth >= 6 && legal >= 12) reduction++;
            if (isPv) reduction--;
            if (reduction < 0) reduction = 0;
        }

        if (!make(p, m)) continue;
        legal++;
        int sc;
        if (legal == 1) {
            sc = -search(p, depth - 1, -beta, -alpha, ply + 1, true);
        } else {
            sc = -search(p, depth - 1 - reduction, -alpha - 1, -alpha, ply + 1, true);
            if (sc > alpha && reduction > 0) sc = -search(p, depth - 1, -alpha - 1, -alpha, ply + 1, true);
            if (sc > alpha && sc < beta) sc = -search(p, depth - 1, -beta, -alpha, ply + 1, true);
        }
        unmake(p, m);
        if (timeUp) return bestScore > -SCORE_INF ? bestScore : 0;

        if (sc > bestScore) {
            bestScore = sc;
            bestMove = m;
            if (sc > alpha) {
                alpha = sc;
                if (sc >= beta) {
                    if (!cap) {
                        if (killers[ply].m[0] != m) { killers[ply].m[1] = killers[ply].m[0]; killers[ply].m[0] = m; }
                        int f = mvFrom(m), t = mvTo(m);
                        historyTab[p.side][f][t] += depth * depth;
                        if (historyTab[p.side][f][t] > 500000) {
                            for (int a = 0; a < 64; a++) for (int b = 0; b < 64; b++) historyTab[p.side][a][b] /= 2;
                        }
                    }
                    break;
                }
            }
        }
    }

    if (legal == 0) return inChk ? (-SCORE_MATE + ply) : SCORE_DRAW;

    int flag = 1;
    if (bestScore >= beta) flag = 2;
    else if (bestScore <= origAlpha) flag = 3;
    Score storeScore = bestScore;
    if (storeScore > SCORE_MATE - 1000) storeScore += ply;
    else if (storeScore < -SCORE_MATE + 1000) storeScore -= ply;
    ttStore(p.hash, depth, storeScore, flag, bestMove);
    return bestScore;
}

static uint32_t bestRootMove;
static Score bestRootScore;

static void iterativeDeepening(Pos &p, const SearchLimits &lim, bool verbose) {
    MoveList root;
    uint32_t raw[256];
    int n = genMoves(p, raw);
    // Learned avoid-list: drop moves that lost material in our own past games.
    // Dropping them from the root list is safe; reordering them to the back is
    // not. Demoting a move broke the aspiration-window/PVS assumptions at the
    // root badly enough that vetoing 1.c4 produced 1.h4, so the move is removed
    // outright instead -- unless that would leave nothing to play.
    const string rootKey = bookKey(p);
    vector<uint32_t> legalRoot;
    for (int i = 0; i < n; i++) {
        if (!make(p, raw[i])) continue;
        unmake(p, raw[i]);
        legalRoot.push_back(raw[i]);
    }
    for (uint32_t m : legalRoot) {
        if (!avoidMoves.empty() && legalRoot.size() > 1 &&
            avoidMoves.count(rootKey + "|" + moveToUci(m)))
            continue;
        root.add(m, 0);
    }
    if (root.n == 0) { bestRootMove = MV_NONE; return; }
    bestRootMove = root.m[0];
    uint32_t stableBest = root.m[0];   // last move from a FULLY completed iteration
    bestRootScore = 0;

    int maxDepth = lim.depth;
    Score prev = 0;
    for (int depth = 1; depth <= maxDepth; depth++) {
        int alpha = -SCORE_INF, beta = SCORE_INF;
        if (depth >= 4) { alpha = prev - 60; beta = prev + 60; }
        Score score = 0;
        while (true) {
            int a = alpha, b = beta;
            // order root moves: previous best first, then previous ordering
            MoveList cur = root;
            for (int i = 0; i < cur.n; i++) cur.score[i] = (cur.m[i] == bestRootMove) ? 1000000 : 0;
            cur.sortDesc();
            Score localBest = -SCORE_INF;
            uint32_t localMove = MV_NONE;
            int legal = 0;
            for (int i = 0; i < cur.n; i++) {
                uint32_t m = cur.m[i];
                if (!make(p, m)) continue;
                legal++;
                Score sc;
                if (legal == 1) sc = -search(p, depth - 1, -b, -a, 1, true);
                else {
                    sc = -search(p, depth - 1, -a - 1, -a, 1, true);
                    if (sc > a && sc < b) sc = -search(p, depth - 1, -b, -a, 1, true);
                }
                unmake(p, m);
                if (timeUp) break;
                if (sc > localBest) { localBest = sc; localMove = m; }
                if (sc > a) a = sc;
            }
            if (timeUp && localMove == MV_NONE) break;
            score = localBest;
            if (localMove != MV_NONE) bestRootMove = localMove;
            if (timeUp) break;
            if (score <= alpha) { alpha = max(-SCORE_INF, score - 200); continue; }
            if (score >= beta) { beta = min(SCORE_INF, score + 200); continue; }
            break;
        }
        if (timeUp) break;
        prev = score;
        bestRootScore = score;
        stableBest = bestRootMove;
        if (verbose) {
            auto now = chrono::steady_clock::now();
            int64_t ms = max<int64_t>(1, chrono::duration_cast<chrono::milliseconds>(now - startTime).count());
            U64 nps = nodesSearched * 1000 / ms;
            string pv = "";
            printf("info depth %d score %s%d nodes %llu nps %llu time %lld pv %s\n",
                   depth,
                   (abs(score) > SCORE_MATE - 1000) ? (score > 0 ? "mate " : "mate -") : "cp ",
                   (int)(abs(score) > SCORE_MATE - 1000 ? (SCORE_MATE - abs(score) + 1) / 2 : score),
                   (unsigned long long)nodesSearched, (unsigned long long)nps, (long long)ms,
                   moveToUci(bestRootMove).c_str());
            fflush(stdout);
        }
        // time management: stop if a big fraction of budget is spent
        auto now = chrono::steady_clock::now();
        int64_t ms = chrono::duration_cast<chrono::milliseconds>(now - startTime).count();
        if (hardDeadline > 0 && ms > hardDeadline * 55 / 100) break;
        if (abs(score) > SCORE_MATE - 200) break;
    }
    // A half-finished iteration must never replace the answer. Its move has been
    // searched with a null window and possibly cut off, so promoting it is how an
    // engine lands on a move it did not actually choose -- vetoing 1.c4 under a
    // 400ms limit produced 1.h4 for exactly this reason.
    bestRootMove = stableBest;
}

// ---------------------------------------------------------------- UCI
static string moveToUci(uint32_t m) {
    if (m == MV_NONE) return "0000";
    string s;
    s += char('a' + sqFile(mvFrom(m)));
    s += char('1' + sqRank(mvFrom(m)));
    s += char('a' + sqFile(mvTo(m)));
    s += char('1' + sqRank(mvTo(m)));
    int promo = mvPromo(m);
    if (promo >= 0) s += " nbrq"[promo];
    return s;
}
static uint32_t uciToMove(const Pos &p, const string &s) {
    uint32_t list[256];
    int n = genMoves(p, list);
    string want = s.substr(0, 4);
    for (int i = 0; i < n; i++) {
        uint32_t m = list[i];
        if (moveToUci(m).substr(0, 4) == want) {
            if (s.size() >= 5) {
                if (mvPromo(m) >= 0 && " nbrq"[mvPromo(m)] == s[4]) return m;
                if (mvPromo(m) >= 0) continue;
            } else if (mvPromo(m) >= 0) continue;
            return m;
        }
    }
    return MV_NONE;
}

int main(int argc, char **argv) {
    // zobrist init
    for (int c = 0; c < 2; c++)
        for (int t = 0; t < 6; t++)
            for (int s = 0; s < 64; s++) Zp[c][t][s] = rnd64();
    for (int i = 0; i < 16; i++) Zcastle[i] = rnd64();
    for (int i = 0; i < 8; i++) Zep[i] = rnd64();
    Zside = rnd64();
    initTables();

    Pos pos;
    posFromFen(pos, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
    ttInit(32);
    if (getenv("SABLE_NO_BOOK")) bookEnabled = false;
    if (bookEnabled) loadBook(resolveDataFile("book.txt", "SABLE_BOOK", "book.txt"));
    loadAvoid(resolveDataFile("avoid.txt", "SABLE_AVOID", "avoid.txt"));
    for (int i = 0; i < 128; i++) killers[i] = Killer();
    memset(historyTab, 0, sizeof(historyTab));

    string line;
    bool goFlag = false;
    while (getline(cin, line)) {
        istringstream ss(line);
        string cmd;
        ss >> cmd;
        if (cmd == "uci") {
            printf("id name Sable 1.0\n");
            printf("id author formalcode\n");
            printf("option name Hash type spin default 32 min 1 max 1024\n");
            printf("option name Threads type spin default 1 min 1 max 1\n");
            printf("option name Book type check default true\n");
            printf("option name BookFile type string default book.txt\n");
            printf("uciok\n");
            fflush(stdout);
        } else if (cmd == "isready") {
            printf("readyok\n");
            fflush(stdout);
        } else if (cmd == "setoption") {
            string a, b, name, value;
            ss >> a; // name
            while (ss >> b && b != "value") name += (name.empty() ? "" : " ") + b;
            if (b == "value") { while (ss >> b) value += (value.empty() ? "" : " ") + b; }
            if (name == "Hash") ttInit((size_t)max(1, atoi(value.c_str())));
            else if (name == "Book") {
                bookEnabled = !(value == "false" || value == "0");
                fprintf(stderr, "info string book %s\n", bookEnabled ? "on" : "off");
            } else if (name == "BookFile") {
                bookMoves.clear();
                loadBook(resolveDataFile(value, "SABLE_BOOK", "book.txt"));
            }
        } else if (cmd == "ucinewgame") {
            ttClear();
            memset(historyTab, 0, sizeof(historyTab));
            for (int i = 0; i < 128; i++) killers[i] = Killer();
            posFromFen(pos, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
        } else if (cmd == "position") {
            string tok;
            ss >> tok;
            if (tok == "startpos") {
                posFromFen(pos, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
                ss >> tok; // maybe 'moves'
            } else if (tok == "fen") {
                string fen, part;
                int cnt = 0;
                while (cnt < 6 && ss >> part) {
                    if (part == "moves") break;
                    fen += (fen.empty() ? "" : " ") + part;
                    cnt++;
                }
                posFromFen(pos, fen);
            }
            while (ss >> tok) {
                if (tok == "moves") continue;
                uint32_t m = uciToMove(pos, tok);
                if (m == MV_NONE) { fprintf(stderr, "info string illegal move %s\n", tok.c_str()); break; }
                if (!make(pos, m)) { fprintf(stderr, "info string illegal move %s\n", tok.c_str()); break; }
            }
        } else if (cmd == "go") {
            SearchLimits lim;
            string tok;
            while (ss >> tok) {
                if (tok == "depth") ss >> lim.depth;
                else if (tok == "movetime") ss >> lim.movetime;
                else if (tok == "wtime") ss >> lim.wtime;
                else if (tok == "btime") ss >> lim.btime;
                else if (tok == "winc") ss >> lim.winc;
                else if (tok == "binc") ss >> lim.binc;
                else if (tok == "nodes") ss >> lim.nodes;
                else if (tok == "movestogo") ss >> lim.movestogo;
                else if (tok == "infinite") lim.infinite = true;
            }
            // Opening book first: instant, and it preserves clock in fast games.
            // Not during `go infinite` (analysis must actually search) and not when
            // the book has been switched off.
            if (bookEnabled && !lim.infinite) {
                auto it = bookMoves.find(bookKey(pos));
                if (it != bookMoves.end()) {
                    uint32_t bm = uciToMove(pos, it->second);
                    if (bm != MV_NONE && make(pos, bm)) {
                        unmake(pos, bm);
                        printf("info string book move\n");
                        printf("bestmove %s\n", moveToUci(bm).c_str());
                        fflush(stdout);
                        continue;
                    }
                }
            }
            startTime = chrono::steady_clock::now();
            timeUp = false;
            nodesSearched = 0;
            TT_AGE++;
            for (int i = 0; i < 128; i++) killers[i] = Killer();
            int64_t budget = -1;
            if (lim.movetime > 0) budget = lim.movetime;
            else if (lim.wtime >= 0) {
                int64_t t = pos.side == WHITE ? lim.wtime : lim.btime;
                int64_t inc = pos.side == WHITE ? lim.winc : lim.binc;
                int mtg = lim.movestogo > 0 ? lim.movestogo : 30;
                budget = max<int64_t>(10, t / mtg + inc * 3 / 4);
                if (budget > t - 50) budget = max<int64_t>(10, t - 50);
            } else if (lim.depth < 128 && lim.nodes < 0) {
                budget = -1; // fixed depth
            }
            hardDeadline = (budget > 0 && !lim.infinite) ? budget : -1;
            nodeLimit = lim.nodes;
            iterativeDeepening(pos, lim, true);
            printf("bestmove %s\n", moveToUci(bestRootMove).c_str());
            fflush(stdout);
        } else if (cmd == "perft") {
            int d = 1;
            ss >> d;
            auto t0 = chrono::steady_clock::now();
            U64 n = perft(pos, d);
            auto t1 = chrono::steady_clock::now();
            int64_t ms = chrono::duration_cast<chrono::milliseconds>(t1 - t0).count();
            printf("perft %d = %llu (%lld ms)\n", d, (unsigned long long)n, (long long)ms);
            fflush(stdout);
        } else if (cmd == "eval") {
            printf("info string eval %d\n", evaluate(pos));
            fflush(stdout);
        } else if (cmd == "d") {
            printf("info string %s\n", posToFen(pos).c_str());
            fflush(stdout);
        } else if (cmd == "quit") {
            break;
        }
    }
    return 0;
}
