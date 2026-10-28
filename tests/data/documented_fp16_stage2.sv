// ============================================================================
// fp16_multiply -- stage-2 normalization restructured (duplicated shifter)
// Strategy:   Duplicate logic: two normalization shifters (shift by ps and by
//             ps+1) driven only by early signals; the late 'c' just picks one.
// What changed: pn = c ? psh<<1 : psh became pn = c ? pshB : psh, where
//             psh = p<<ps and pshB = p<<(ps+1). Everything else is unchanged.
// Why it helps: the late, multiplier-derived 'c' no longer feeds a 1-bit
//             shift stage after the barrel shifter's output; ps+1 comes from
//             early lzc signals, off the multiplier path. Expected to shorten
//             the b1 -> pn2 path.
// Equivalence: pn is bit-identical (22-bit shift by ps+1 equals shift by ps
//             then by 1, truncating; ps <= 20 so ps+1 fits 5 bits). Registers,
//             reset and outputs are unchanged.
// Latency:    cycle-exact
// Tradeoffs:  an extra 22-bit shifter (more area); c still fans out to 22 muxes.
// ============================================================================
module fp16_multiply (
  input  logic        clk,
  input  logic        rst,
  input  logic [15:0] a,
  input  logic [15:0] b,
  input  logic        en,
  output logic [15:0] y,
  output logic        valid
);
  // Stage 1: input registers
  logic [15:0] a1, b1;
  logic        v1;
  always_ff @(posedge clk) begin
    a1 <= a;
    b1 <= b;
    v1 <= rst ? 1'b0 : en;
  end

  // Stage 2 signals: unpacked fields, special flags, product and shifts
  logic [10:0] ma, mb;
  logic [5:0]  ea, eb;
  logic        a_zero, b_zero, a_inf, b_inf, a_nan, b_nan;
  logic [21:0] p, psh, pshB, pn;   // OPT: pshB = p<<(ps+1)
  logic [3:0]  la, lb;
  logic [4:0]  ps, ps1;            // OPT: ps1 = ps+1, early
  logic        c, cb;
  logic signed [9:0] E, E0, E1;

  // Stage 2 logic: multiply raw mantissas; normalization shift in parallel
  always_comb begin
    ea = (a1[14:10] == 5'd0) ? 6'd1 : {1'b0, a1[14:10]};
    eb = (b1[14:10] == 5'd0) ? 6'd1 : {1'b0, b1[14:10]};
    ma = {(a1[14:10] != 5'd0), a1[9:0]};
    mb = {(b1[14:10] != 5'd0), b1[9:0]};
    a_zero = (a1[14:0] == 15'd0);
    b_zero = (b1[14:0] == 15'd0);
    a_inf  = (a1[14:10] == 5'h1F) && (a1[9:0] == 10'd0);
    b_inf  = (b1[14:10] == 5'h1F) && (b1[9:0] == 10'd0);
    a_nan  = (a1[14:10] == 5'h1F) && (a1[9:0] != 10'd0);
    b_nan  = (b1[14:10] == 5'h1F) && (b1[9:0] != 10'd0);
    la = 4'd0;
    lb = 4'd0;
    for (int i = 0; i < 11; i++) begin
      if (ma[i]) la = 4'(10 - i);
      if (mb[i]) lb = 4'(10 - i);
    end
    p   = ma * mb;
    ps  = 5'(la) + 5'(lb);
    ps1 = 5'(la) + 5'(lb) + 5'd1;  // OPT: early ps+1 (ps<=20, no overflow)
    psh  = p << ps;
    pshB = p << ps1;               // OPT: duplicate shifter, one extra position
    // psh[21] == p[21-ps] (ps <= 20): select with early-decoded ps
    cb = 1'b0;
    for (int k = 0; k <= 20; k++) cb = cb | ((ps == 5'(k)) & p[21-k]);
    c   = ~cb;
    pn  = c ? pshB : psh;          // OPT: late select between precomputed shifts
    // both exponent candidates depend only on early signals
    E0  = 10'(signed'({4'b0, ea}) + signed'({4'b0, eb}) - 10'sd14
              - signed'({6'b0, la}) - signed'({6'b0, lb}));
    E1  = 10'(signed'({4'b0, ea}) + signed'({4'b0, eb}) - 10'sd15
              - signed'({6'b0, la}) - signed'({6'b0, lb}));
    E   = c ? E1 : E0;
  end

  // Stage 2 -> 3 pipeline registers
  logic [21:0] pn2;
  logic signed [9:0] E2;
  logic        s2, nan2, inf2, zero2, v2;
  always_ff @(posedge clk) begin
    pn2   <= pn;
    E2    <= E;
    s2    <= a1[15] ^ b1[15];
    nan2  <= a_nan | b_nan | (a_inf & b_zero) | (b_inf & a_zero);
    inf2  <= a_inf | b_inf;
    zero2 <= a_zero | b_zero;
    v2    <= rst ? 1'b0 : v1;
  end

  // Stage 3: round + pack (unchanged)
  logic [21:0] sh_v;
  logic [4:0]  sh;
  logic        lost, g, st, up;
  logic [14:0] base, sum;
  logic [15:0] res;

  always_comb begin
    if (E2 >= 10'sd1) sh = 5'd0;
    else if (E2 <= -10'sd23) sh = 5'd24;
    else sh = 5'(10'sd1 - E2);
    if (sh >= 5'd22) begin
      sh_v = 22'd0;
      lost = |pn2;
    end else begin
      sh_v = pn2 >> sh;
      lost = |(pn2 & ~(22'h3FFFFF << sh));
    end
    g  = sh_v[10];
    st = (|sh_v[9:0]) | lost;
    up = g & (st | sh_v[11]);
    base = {(E2 >= 10'sd1) ? E2[4:0] : 5'd0, sh_v[20:11]};
    sum = base + 15'(up);
    if (nan2) res = 16'h7E00;
    else if (inf2) res = {s2, 15'h7C00};
    else if (zero2) res = {s2, 15'd0};
    else if (E2 >= 10'sd31 || sum >= 15'h7C00) res = {s2, 15'h7C00};
    else res = {s2, sum};
  end

  // Output registers
  always_ff @(posedge clk) begin
    y     <= res;
    valid <= rst ? 1'b0 : v2;
  end
endmodule
