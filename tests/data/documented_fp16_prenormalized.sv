// ============================================================================
// fp16_multiply -- IEEE binary16 multiplier, 3-stage pipeline, operands pre-normalized
// Strategy:   Move subnormal handling in front of the multiplier: left-justify each
//             mantissa with a log shifter (which also yields the leading-zero count),
//             so the product's normalization is a single-bit test of p[21].
// What changed: Stage-2 only. Removed the post-multiply variable shifter (p << (la+lb)),
//             the 5-bit la+lb adder feeding it, and the 21-way "cb" select tree.
//             Multiplier now takes normalized operands; pn = p[21] ? p : p<<1.
//             Exponent candidates E0/E1 and the stage-3 logic are unchanged.
// Why it helps: The reported critical path is multiplier -> (la+lb) shift -> normalize
//             select -> pn2. The shifter, adder and wide select are gone from behind the
//             multiplier; the small lzc/shift runs ahead of it, and its la/lb outputs also
//             feed the exponent adders in parallel. Expected shorter logic depth.
// Equivalence: (ma<<la)*(mb<<lb) == (ma*mb)<<(la+lb) exactly (fits in 22 bits), so pn
//             is identical to the old psh-based value; c = ~psh[21] = ~p[21]. Same registers,
//             same reset behaviour.
// Latency:    cycle-exact
// Tradeoffs:  lzc+shift is serial ahead of the multiplier (two small shifters); area
//             should shrink overall but multiplier inputs arrive later.
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
  // Stage 1: input registers (unchanged)
  logic [15:0] a1, b1;
  logic        v1;
  always_ff @(posedge clk) begin
    a1 <= a;
    b1 <= b;
    v1 <= rst ? 1'b0 : en;
  end

  // OPT: function that left-justifies an 11-bit mantissa and returns {lz[3:0], shifted[10:0]}
  // using a 8/4/2/1 log shifter; the shift decisions are the leading-zero count bits.
  function automatic logic [14:0] norm(input logic [10:0] m);
    logic [10:0] t;
    logic [3:0]  l;
    t = m;
    l[3] = (t[10:3] == 8'd0);
    if (l[3]) t = t << 8;
    l[2] = (t[10:7] == 4'd0);
    if (l[2]) t = t << 4;
    l[1] = (t[10:9] == 2'd0);
    if (l[1]) t = t << 2;
    l[0] = ~t[10];
    if (l[0]) t = t << 1;
    return {l, t};
  endfunction

  // Stage 2 signals
  logic [10:0] ma, mb, man, mbn;
  logic [5:0]  ea, eb;
  logic        a_zero, b_zero, a_inf, b_inf, a_nan, b_nan;
  logic [21:0] p, pn;
  logic [3:0]  la, lb;
  logic        c;
  logic signed [9:0] E, E0, E1;

  // Special-case decode, raw mantissas, exponents, normalization and product.
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

    // OPT: pre-normalize operands so the product MSB is at bit 21 or 20
    {la, man} = norm(ma);
    {lb, mbn} = norm(mb);
    p = man * mbn;

    // OPT: normalization of product is a single-bit test (no variable shift behind the multiplier)
    c  = ~p[21];
    pn = c ? {p[20:0], 1'b0} : p;

    // Both exponent candidates depend only on early signals; select by c at the end.
    E0 = 10'(signed'({4'b0, ea}) + signed'({4'b0, eb}) - 10'sd14
             - signed'({6'b0, la}) - signed'({6'b0, lb}));
    E1 = 10'(signed'({4'b0, ea}) + signed'({4'b0, eb}) - 10'sd15
             - signed'({6'b0, la}) - signed'({6'b0, lb}));
    E  = c ? E1 : E0;
  end

  // Stage 2 -> 3 registers (unchanged)
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

  // Output registers (unchanged)
  always_ff @(posedge clk) begin
    y     <= res;
    valid <= rst ? 1'b0 : v2;
  end
endmodule
