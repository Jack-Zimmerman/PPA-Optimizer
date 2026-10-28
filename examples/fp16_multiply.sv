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

  // Stage 2: multiply + normalize
  logic [10:0] ma, mb;
  logic [5:0]  ea, eb;
  logic        a_zero, b_zero, a_inf, b_inf, a_nan, b_nan;
  logic [21:0] p, pn;
  logic [4:0]  lz;
  logic signed [9:0] E;

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
    p  = ma * mb;
    lz = 5'd0;
    for (int i = 0; i < 22; i++)
      if (p[i]) lz = 5'(21 - i);
    pn = p << lz;
    E  = 10'(signed'({4'b0, ea}) + signed'({4'b0, eb}) - 10'sd14 - signed'({5'b0, lz}));
  end

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

  // Stage 3: round + pack
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
    // normal case: sh_v[21] is hidden bit, implicit via exponent field
    sum = base + 15'(up);
    if (nan2) res = 16'h7E00;
    else if (inf2) res = {s2, 15'h7C00};
    else if (zero2) res = {s2, 15'd0};
    else if (E2 >= 10'sd31 || sum >= 15'h7C00) res = {s2, 15'h7C00};
    else res = {s2, sum};
  end

  always_ff @(posedge clk) begin
    y     <= res;
    valid <= rst ? 1'b0 : v2;
  end
endmodule
